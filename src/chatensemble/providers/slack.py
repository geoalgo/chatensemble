"""Slack provider (Web API, user token ``xoxp-``)."""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import httpx

from ..base import ChatClient, ChatClientError
from ..config import AccountConfig
from ..filters import FetchFilter
from ..models import Channel, ChannelKind, Message, from_epoch_s

_API = "https://slack.com/api"

# per-method sliding-window caps (req / 60s), a bit under Slack's real limits so
# concurrent workers throttle themselves instead of provoking 429s
_METHOD_LIMITS = {
    "conversations.history": 45,   # Tier 3 (~50/min)
    "conversations.replies": 45,   # Tier 3, separate bucket
    "conversations.list": 18,      # Tier 2 (~20/min)
}


class _RateLimiter:
    """Thread-safe: at most ``per_minute`` :meth:`acquire` calls per rolling 60s."""

    def __init__(self, per_minute: int) -> None:
        self._max = per_minute
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._hits and self._hits[0] <= now - 60.0:
                    self._hits.popleft()
                if len(self._hits) < self._max:
                    self._hits.append(now)
                    return
                sleep_for = self._hits[0] + 60.0 - now
            time.sleep(max(sleep_for, 0.01))
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
_CHAN_RE = re.compile(r"<#[A-Z0-9]+\|([^>]+)>")
_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
_BARE_LINK_RE = re.compile(r"<(https?://[^>]+)>")
_SPECIAL_RE = re.compile(r"<!(here|channel|everyone)>")


class SlackClient(ChatClient):
    provider = "slack"

    def __init__(self, account: AccountConfig) -> None:
        super().__init__(account)
        self.token = str(account.get("token", required=True))
        # Slack has no username/password API. The "log in as me" path is a browser
        # session: an xoxc- token plus its "d" cookie (xoxd-...), both copied from
        # a logged-in Slack tab. App tokens (xoxp-/xoxb-) need no cookie.
        cookie = account.get("cookie")
        if isinstance(cookie, str) and cookie.strip().lower().startswith("d="):
            cookie = cookie.split("=", 1)[1]
        self.cookie = cookie.strip() if isinstance(cookie, str) else None

        if not self.token.startswith(("xoxp-", "xoxb-", "xoxc-")):
            raise ChatClientError(
                f"account {self.name!r}: slack needs a token starting with xoxp-/xoxb- "
                "(app token) or xoxc- (browser token, with a 'cookie:' value too)"
            )
        if self.token.startswith("xoxc-") and not self.cookie:
            raise ChatClientError(
                f"account {self.name!r}: an xoxc- token also needs the browser 'd' "
                "cookie in 'cookie:' (its value starts with xoxd-)"
            )
        self._me: dict = {}
        self._team: dict = {}
        self._users: dict[str, str] = {}
        self._last_read: dict[str, float] = {}
        # {channel_id: ts of its newest message}, from one client.counts probe;
        # None once we know the probe is unavailable (e.g. an xoxp- app token).
        self._latest: dict[str, float] | None = {}
        self._latest_probed = False
        # per-channel history fetch runs in a small thread pool
        self._concurrency = max(1, min(12, int(account.get("concurrency", 6) or 6)))
        self._rl = {m: _RateLimiter(n) for m, n in _METHOD_LIMITS.items()}

    # ------------------------------------------------------------------ #
    def _build_http(self) -> httpx.Client:
        return httpx.Client(
            base_url=_API,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "chatensemble/0.1",
            },
            cookies={"d": self.cookie} if self.cookie else None,
        )

    def _call(self, method: str, **params) -> dict:
        """GET a Slack Web API method. Self-throttles per method and waits out
        429s (bounded by a total-wait cap, not a fixed retry count)."""
        limiter = self._rl.get(method)
        query = {k: v for k, v in params.items() if v is not None}
        waited = 0.0
        while True:
            if limiter is not None:
                limiter.acquire()
            resp = self.http.get(f"/{method}", params=query)
            if resp.status_code == 429 or (
                resp.is_success and resp.json().get("error") == "ratelimited"
            ):
                wait = min(int(resp.headers.get("Retry-After", "2") or 2), 60)
                waited += wait
                if waited > 180:
                    raise ChatClientError(f"slack {method}: rate-limited > 180s")
                time.sleep(wait)
                continue
            if not resp.is_success:
                raise ChatClientError(f"slack {method} HTTP {resp.status_code}")
            data = resp.json()
            if not data.get("ok"):
                raise ChatClientError(f"slack {method} error: {data.get('error', 'unknown')}")
            return data

    def _post(self, method: str, **params) -> dict:
        """POST a Slack Web API write method (token is in the auth header)."""
        resp = self.http.post(
            f"/{method}", data={k: v for k, v in params.items() if v is not None}
        )
        if not resp.is_success:
            raise ChatClientError(f"slack {method} HTTP {resp.status_code}")
        data = resp.json()
        if not data.get("ok"):
            raise ChatClientError(f"slack {method} error: {data.get('error', 'unknown')}")
        return data

    def _paged(self, method: str, key: str, **params) -> Iterator[dict]:
        cursor = None
        while True:
            data = self._call(method, cursor=cursor, limit=200, **params)
            yield from data.get(key, [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return

    # ------------------------------------------------------------------ #
    def authenticate(self) -> None:
        self._me = self._call("auth.test")
        self._team = {"id": self._me.get("team_id"), "url": self._me.get("url", "")}
        # best-effort bulk user directory
        try:
            for u in self._paged("users.list", "members"):
                self._users[u["id"]] = (
                    u.get("profile", {}).get("display_name")
                    or u.get("real_name")
                    or u.get("name")
                    or u["id"]
                )
        except ChatClientError:
            pass

    # ------------------------------------------------------------------ #
    def _user_name(self, uid: str | None) -> str:
        if not uid:
            return "(system)"
        if uid not in self._users:
            try:
                info = self._call("users.info", user=uid)["user"]
                self._users[uid] = (
                    info.get("profile", {}).get("display_name")
                    or info.get("real_name")
                    or info.get("name")
                    or uid
                )
            except ChatClientError:
                self._users[uid] = uid
        return self._users[uid]

    def _prettify(self, text: str) -> str:
        text = _MENTION_RE.sub(lambda m: "@" + self._user_name(m.group(1)), text)
        text = _CHAN_RE.sub(lambda m: "#" + m.group(1), text)
        text = _LINK_RE.sub(lambda m: m.group(2), text)
        text = _BARE_LINK_RE.sub(lambda m: m.group(1), text)
        text = _SPECIAL_RE.sub(lambda m: "@" + m.group(1), text)
        return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")

    def _activity(self) -> dict[str, float] | None:
        """``{channel_id: last-activity ts}`` -- a cheap "does this channel have
        anything new" probe, so :meth:`_iter_channel_between` can skip a
        ``conversations.history`` round trip for every dormant channel.

        Built from two calls a browser (``xoxc-``) token can make: ``client.counts``
        (precise ``latest`` message ts for conversations with unread state) and
        ``client.userBoot`` (coarser ``updated`` ms, but wider coverage). A channel
        absent from both is scanned normally. Returns ``None`` if neither method
        is available (e.g. an ``xoxp-`` app token) -- then everything is scanned.
        """
        if self._latest_probed:
            return self._latest
        self._latest_probed = True

        act: dict[str, float] = {}
        try:
            data = self._call("client.counts")
        except ChatClientError:
            data = None
        for group in ("channels", "mpims", "ims"):
            for c in (data or {}).get(group) or []:
                cid = c.get("id")
                if not cid:
                    continue
                if c.get("latest"):
                    act[cid] = float(c["latest"])
                lr = c.get("last_read")
                if lr and cid not in self._last_read:
                    self._last_read[cid] = float(lr)

        try:
            boot = self._call("client.userBoot")
        except ChatClientError:
            boot = None
        for c in (boot or {}).get("channels") or []:
            cid, upd = c.get("id"), c.get("updated")
            if cid and cid not in act and upd:
                act[cid] = float(upd) / 1000.0     # `updated` is milliseconds

        self._latest = None if (data is None and boot is None) else act
        return self._latest

    def _channel_last_read(self, channel_id: str) -> float:
        if channel_id not in self._last_read:
            try:
                info = self._call("conversations.info", channel=channel_id)["channel"]
                self._last_read[channel_id] = float(info.get("last_read", "0") or 0)
            except ChatClientError:
                self._last_read[channel_id] = 0.0
        return self._last_read[channel_id]

    # ------------------------------------------------------------------ #
    def list_channels(self, flt: FetchFilter | None = None) -> list[Channel]:
        self.ensure_auth()
        out: list[Channel] = []
        for c in self._paged(
            "conversations.list",
            "channels",
            types="public_channel,private_channel,mpim,im",
            exclude_archived="true",
        ):
            if c.get("is_im"):
                kind = ChannelKind.DIRECT
                name = self._user_name(c.get("user"))
            elif c.get("is_mpim"):
                kind = ChannelKind.GROUP
                name = c.get("name", c["id"])
            elif c.get("is_private"):
                kind = ChannelKind.PRIVATE
                name = c.get("name", c["id"])
            else:
                kind = ChannelKind.PUBLIC
                name = c.get("name", c["id"])
                if not c.get("is_member"):
                    continue  # can't read history we're not in

            last_read = c.get("last_read")
            if last_read is not None:
                self._last_read[c["id"]] = float(last_read or 0)
            unread = int(c.get("unread_count_display", c.get("unread_count", 0)) or 0)

            out.append(
                Channel(
                    id=c["id"],
                    name=name,
                    kind=kind,
                    account=self.name,
                    provider=self.provider,
                    team_id=self._team.get("id"),
                    unread_count=unread,
                    last_read_at=from_epoch_s(self._last_read[c["id"]])
                    if self._last_read.get(c["id"])
                    else None,
                    raw=c,
                )
            )
        return out

    # ------------------------------------------------------------------ #
    def messages_between(
        self,
        start: datetime,
        end: datetime,
        channels: Sequence[Channel] | None = None,
        *,
        on_channel=None,
    ) -> Iterator[Message]:
        """Like the base version, but scans channels in a small thread pool.

        Slack costs one ``conversations.history`` round trip per channel (plus a
        ``conversations.replies`` per active thread); serially that dominates the
        fetch. ``self._rl`` keeps the concurrent workers under Slack's per-method
        rate limits, so parallelism trims wall time without provoking 429s.
        """
        self.ensure_auth()
        if channels is None:
            channels = self.list_channels()
        self._activity()                       # probe once, before the pool starts
        total = len(channels)
        if self._concurrency <= 1 or total <= 1:
            yield from super().messages_between(start, end, channels, on_channel=on_channel)
            return

        def collect(ch: Channel):
            try:
                return list(self._iter_channel_between(ch, start, end))
            except ChatClientError as exc:      # one bad channel -> skip, keep the rest
                return exc

        done = 0
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            futures = {pool.submit(collect, ch): ch for ch in channels}
            for fut in as_completed(futures):
                done += 1
                if on_channel is not None:
                    on_channel(futures[fut], done, total)
                result = fut.result()
                if not isinstance(result, ChatClientError):
                    yield from result

    # ------------------------------------------------------------------ #
    def mark_read(self, channel_id: str, *, up_to: datetime | None = None) -> None:
        """Mark ``channel_id`` read up to ``up_to`` (default: its latest message)
        via ``conversations.mark``."""
        self.ensure_auth()
        if up_to is not None:
            ts = f"{up_to.timestamp():.6f}"
        else:
            hist = self._call("conversations.history", channel=channel_id, limit=1)
            msgs = hist.get("messages", [])
            if not msgs:
                return
            ts = msgs[0]["ts"]
        self._post("conversations.mark", channel=channel_id, ts=ts)
        self._last_read[channel_id] = float(ts)

    # ------------------------------------------------------------------ #
    def _iter_channel_between(
        self, channel: Channel, start: datetime, end: datetime
    ) -> Iterator[Message]:
        """Messages with ``start <= ts < end`` (Slack's ``oldest`` / ``latest``).

        Thread replies whose own timestamp falls in the window are included;
        replies to a parent outside the window are only picked up when their
        month is (re)fetched.
        """
        self.ensure_auth()
        start_ts = start.timestamp()

        # (1) skip the whole channel if its newest message predates the window
        act = self._activity()
        if act is not None:
            newest = act.get(channel.id)
            if newest is not None and newest < start_ts:
                return

        last_read = self._channel_last_read(channel.id)
        oldest = f"{start_ts:.6f}"
        latest = f"{end.timestamp():.6f}"

        cursor = None
        while True:
            data = self._call(
                "conversations.history",
                channel=channel.id,
                oldest=oldest,
                latest=latest,
                inclusive="false",
                limit=200,
                cursor=cursor,
            )
            for m in data.get("messages", []):
                if m.get("type") != "message":
                    continue
                yield self._to_message(m, channel, last_read)
                # (2) only fetch replies for threads still active in the window
                if int(m.get("reply_count", 0)) > 0:
                    lr = m.get("latest_reply")
                    if lr is None or float(lr) >= start_ts:
                        yield from self._thread_replies(
                            m["ts"], channel, last_read, start, end
                        )

            cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
            if not data.get("has_more") or not cursor:
                return

    def _thread_replies(
        self,
        thread_ts: str,
        channel: Channel,
        last_read: float,
        start: datetime,
        end: datetime,
    ) -> Iterator[Message]:
        data = self._call("conversations.replies", channel=channel.id, ts=thread_ts, limit=200)
        lo, hi = start.timestamp(), end.timestamp()
        for m in data.get("messages", []):
            if m.get("ts") == thread_ts or m.get("type") != "message":
                continue
            if not (lo <= float(m["ts"]) < hi):
                continue
            yield self._to_message(m, channel, last_read)

    def _to_message(self, m: dict, channel: Channel, last_read: float) -> Message:
        ts = float(m["ts"])
        uid = m.get("user") or m.get("bot_id")
        author = self._user_name(m.get("user")) if m.get("user") else (m.get("username") or "bot")
        is_own = m.get("user") == self._me.get("user_id")
        raw_text = m.get("text", "")
        my_ref = f"<@{self._me.get('user_id')}>"
        mention = (
            channel.kind == ChannelKind.DIRECT
            or my_ref in raw_text
            or "<!here>" in raw_text
            or "<!channel>" in raw_text
            or "<!everyone>" in raw_text
        )
        reactions = {r["name"]: r.get("count", 0) for r in m.get("reactions", [])}
        ts_compact = m["ts"].replace(".", "")
        permalink = (
            f"{self._team.get('url', '').rstrip('/')}/archives/{channel.id}/p{ts_compact}"
            if self._team.get("url")
            else None
        )
        return Message(
            id=m["ts"],
            text=self._prettify(raw_text),
            timestamp=from_epoch_s(ts),
            author_id=uid or "",
            author_name=author,
            channel_id=channel.id,
            channel_name=channel.name,
            channel_kind=channel.kind,
            account=self.name,
            provider=self.provider,
            thread_id=m.get("thread_ts") or m["ts"],
            reply_count=int(m.get("reply_count", 0) or 0),
            is_unread=(not is_own) and last_read > 0 and ts > last_read,
            is_mention=bool(mention) and not is_own,
            is_own=is_own,
            edited=bool(m.get("edited")),
            permalink=permalink,
            reactions=reactions,
            raw=m,
        )
