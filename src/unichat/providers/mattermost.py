"""Mattermost provider (REST API v4)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import httpx

from ..base import ChatClient, ChatClientError
from ..config import AccountConfig
from ..filters import FetchFilter
from ..models import Channel, ChannelKind, Message, from_epoch_ms

_KIND = {
    "O": ChannelKind.PUBLIC,
    "P": ChannelKind.PRIVATE,
    "D": ChannelKind.DIRECT,
    "G": ChannelKind.GROUP,
}
_PAGE = 200


class MattermostClient(ChatClient):
    provider = "mattermost"

    def __init__(self, account: AccountConfig) -> None:
        super().__init__(account)
        raw_server = account.get("server") or account.get("url")
        if not raw_server:
            raise ChatClientError(f"account {self.name!r}: 'server' is required for mattermost")
        self.server = str(raw_server).rstrip("/")
        self.api = f"{self.server}/api/v4"
        self._verify = bool(account.get("verify_ssl", True))
        self._teams_allow = {t.lower() for t in (account.get("teams") or [])}
        self._me: dict = {}
        self._users: dict[str, dict] = {}
        self._teams: list[dict] = []
        self._members: dict[str, dict] = {}   # channel_id -> ChannelMember
        self._team_by_id: dict[str, dict] = {}
        # channel_id -> (window_start_already_scanned, oldest_post_id_seen)
        # lets an adjacent, older window resume instead of re-paging from "now".
        self._scan_cursor: dict[str, tuple[datetime, str | None]] = {}

    # ------------------------------------------------------------------ #
    def _build_http(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.api,
            timeout=30.0,
            verify=self._verify,
            headers={"User-Agent": "unichat/0.1"},
            follow_redirects=True,
        )

    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        if resp.is_success:
            return resp
        detail = ""
        try:
            detail = resp.json().get("message", "")
        except Exception:  # pragma: no cover - defensive
            detail = resp.text[:200]
        raise ChatClientError(f"mattermost {what} failed [{resp.status_code}]: {detail}")

    # ------------------------------------------------------------------ #
    def authenticate(self) -> None:
        token = self.account.get("token")
        if token:
            self.http.headers["Authorization"] = f"Bearer {token}"
        else:
            login_id = self.account.get("username", required=True)
            password = self.account.get("password", required=True)
            resp = self.http.post(
                "/users/login",
                json={"login_id": login_id, "password": password},
            )
            self._check(resp, "login")
            session = resp.headers.get("Token")
            if not session:
                raise ChatClientError("mattermost login: no session token in response")
            self.http.headers["Authorization"] = f"Bearer {session}"

        self._me = self._check(self.http.get("/users/me"), "users/me").json()
        self._users[self._me["id"]] = self._me
        self._teams = self._check(self.http.get("/users/me/teams"), "users/me/teams").json()
        if self._teams_allow:
            self._teams = [t for t in self._teams if t["name"].lower() in self._teams_allow]
        self._team_by_id = {t["id"]: t for t in self._teams}

    # ------------------------------------------------------------------ #
    def _resolve_users(self, ids: set[str]) -> None:
        missing = [i for i in ids if i and i not in self._users]
        for start in range(0, len(missing), 100):
            chunk = missing[start : start + 100]
            resp = self.http.post("/users/ids", json=chunk)
            if resp.is_success:
                for u in resp.json():
                    self._users[u["id"]] = u

    def _display_name(self, user_id: str) -> str:
        u = self._users.get(user_id)
        if not u:
            return user_id
        nick = (u.get("nickname") or "").strip()
        if nick:
            return nick
        full = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
        return full or u.get("username") or user_id

    def _dm_name(self, ch: dict) -> str:
        # direct channel name is "<uid>__<uid>"
        parts = ch.get("name", "").split("__")
        other = next((p for p in parts if p != self._me["id"]), None)
        if other:
            self._resolve_users({other})
            return self._display_name(other)
        return ch.get("display_name") or ch.get("name", "")

    # ------------------------------------------------------------------ #
    def list_channels(self, flt: FetchFilter | None = None) -> list[Channel]:
        self.ensure_auth()
        seen: dict[str, Channel] = {}
        for team in self._teams:
            tid = team["id"]
            members = self.http.get(
                f"/users/me/teams/{tid}/channels/members", params={"per_page": _PAGE}
            )
            if members.is_success:
                for m in members.json():
                    self._members[m["channel_id"]] = m

            page = 0
            while True:
                resp = self._check(
                    self.http.get(
                        f"/users/me/teams/{tid}/channels",
                        params={"per_page": _PAGE, "page": page},
                    ),
                    "list channels",
                )
                batch = resp.json()
                for ch in batch:
                    if ch["id"] in seen:
                        continue
                    seen[ch["id"]] = self._to_channel(ch, team)
                if len(batch) < _PAGE:
                    break
                page += 1
        return list(seen.values())

    def _to_channel(self, ch: dict, team: dict) -> Channel:
        kind = _KIND.get(ch.get("type", ""), ChannelKind.UNKNOWN)
        if kind == ChannelKind.DIRECT:
            name = self._dm_name(ch)
        elif kind == ChannelKind.GROUP:
            name = ch.get("display_name") or ch.get("name", "")
        else:
            name = ch.get("name") or ch.get("display_name", "")

        member = self._members.get(ch["id"], {})
        total = ch.get("total_msg_count", 0)
        seen_count = member.get("msg_count", total)
        last_viewed = member.get("last_viewed_at", 0)
        return Channel(
            id=ch["id"],
            name=name,
            kind=kind,
            account=self.name,
            provider=self.provider,
            team_id=team["id"],
            team_name=team["name"],
            unread_count=max(0, total - seen_count),
            mention_count=member.get("mention_count", 0),
            last_read_at=from_epoch_ms(last_viewed) if last_viewed else None,
            raw=ch,
        )

    # ------------------------------------------------------------------ #
    def mark_read(self, channel_id: str, *, up_to: datetime | None = None) -> None:
        """Mark ``channel_id`` read. Mattermost's marker is channel-level: this
        marks the whole channel viewed up to its latest post, so ``up_to`` is
        ignored."""
        self.ensure_auth()
        self._check(
            self.http.post(
                "/channels/members/me/view",
                json={"channel_id": channel_id},
            ),
            "mark channel read",
        )
        # keep the in-memory member marker roughly in sync for this session
        member = self._members.get(channel_id)
        if member is not None:
            member["last_viewed_at"] = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    # ------------------------------------------------------------------ #
    def _iter_channel_between(
        self, channel: Channel, start: datetime, end: datetime
    ) -> Iterator[Message]:
        """Messages with ``start <= ts < end``.

        Mattermost's ``/posts`` endpoint has a lower bound (``since``) but no
        upper bound, so we page newest->oldest, discarding anything ``>= end``
        and stopping once we drop below ``start``. When the previous call for
        this channel scanned down to exactly ``end`` we resume from the post id
        it stopped on instead of paging from "now" again.

        Channels whose ``last_post_at`` (from the channel list) predates the
        window are skipped without any HTTP request at all.
        """
        self.ensure_auth()

        start_ms = int(start.timestamp() * 1000)
        last_post_at = int(channel.raw.get("last_post_at") or 0)
        if last_post_at and last_post_at < start_ms:
            return

        member = self._members.get(channel.id, {})
        last_viewed = member.get("last_viewed_at", 0)
        my_username = self._me.get("username", "")

        resume = self._scan_cursor.get(channel.id)
        before: str | None = resume[1] if resume and resume[0] == end else None
        oldest_seen: str | None = before

        while True:
            params: dict[str, object] = {"per_page": _PAGE}
            if before:
                params["before"] = before
            resp = self._check(
                self.http.get(f"/channels/{channel.id}/posts", params=params),
                f"posts for {channel.name}",
            )
            data = resp.json()
            order: list[str] = data.get("order", [])
            posts: dict[str, dict] = data.get("posts", {})
            if not order:
                break

            self._resolve_users({posts[pid]["user_id"] for pid in order if pid in posts})

            stop = False
            for pid in order:
                post = posts.get(pid)
                if not post or post.get("delete_at"):
                    continue
                ts = from_epoch_ms(post["create_at"])
                if ts >= end:
                    continue                       # newer than the window; keep paging back
                if ts < start:
                    stop = True
                    break
                yield self._to_message(post, channel, last_viewed, my_username)
            oldest_seen = order[-1]
            if stop:
                break

            nxt = data.get("prev_post_id") or order[-1]
            if not nxt or nxt == before or len(order) < _PAGE:
                break
            before = nxt

        self._scan_cursor[channel.id] = (start, oldest_seen)

    def _to_message(
        self, post: dict, channel: Channel, last_viewed: int, my_username: str
    ) -> Message:
        author_id = post["user_id"]
        text = post.get("message", "")
        is_own = author_id == self._me["id"]
        root = post.get("root_id") or ""
        create_at = post["create_at"]

        mention = (
            channel.kind in (ChannelKind.DIRECT, ChannelKind.GROUP)
            or (my_username and f"@{my_username}" in text)
            or any(tag in text for tag in ("@all", "@here", "@channel"))
        )
        reactions: dict[str, int] = {}
        for r in (post.get("metadata") or {}).get("reactions", []) or []:
            reactions[r["emoji_name"]] = reactions.get(r["emoji_name"], 0) + 1

        permalink = None
        if channel.team_name:
            permalink = f"{self.server}/{channel.team_name}/pl/{post['id']}"

        return Message(
            id=post["id"],
            text=text,
            timestamp=from_epoch_ms(create_at),
            author_id=author_id,
            author_name=self._display_name(author_id),
            channel_id=channel.id,
            channel_name=channel.name,
            channel_kind=channel.kind,
            account=self.name,
            provider=self.provider,
            thread_id=root or post["id"],
            reply_count=int(post.get("reply_count", 0) or 0),
            is_unread=(not is_own) and last_viewed > 0 and create_at > last_viewed,
            is_mention=bool(mention) and not is_own,
            is_own=is_own,
            edited=bool(post.get("edit_at")),
            permalink=permalink,
            reactions=reactions,
            raw=post,
        )
