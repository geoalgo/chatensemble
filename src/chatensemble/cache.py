"""Month-partitioned message cache.

Layout (``<root>`` defaults to ``~/chatensemble/cache``)::

    <root>/<account>/2026-07.parquet   every message, all channels
    <root>/<account>/2026-08.parquet
    <root>/<account>/_meta.json        {"2026-07": {"fetched_at": ..., "count": ...}}

The current month is always re-fetched and overwritten; past months are served
from disk. With no lower bound and no cache yet for an account, the first fetch
walks backwards a year at a time and stops at the first fully empty year.

Nothing in here is provider-specific: it only calls
:meth:`ChatClient.list_channels` and :meth:`ChatClient.messages_between`.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .base import ChatClient
from .filters import FetchFilter, cap_per_channel
from .models import Channel, Message
from .progress import Reporter, as_reporter
from .serde import read_parquet, write_parquet

UTC = timezone.utc
DEFAULT_CACHE_ROOT = Path.home() / "chatensemble" / "cache"
_EXT = ".parquet"
_MIN_YEAR = 2010


# --------------------------------------------------------------------------- #
# month / year arithmetic
# --------------------------------------------------------------------------- #
def month_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def month_start(key: str) -> datetime:
    year, month = (int(p) for p in key.split("-"))
    return datetime(year, month, 1, tzinfo=UTC)


def month_end(key: str) -> datetime:
    start = month_start(key)
    if start.month == 12:
        return datetime(start.year + 1, 1, 1, tzinfo=UTC)
    return datetime(start.year, start.month + 1, 1, tzinfo=UTC)


def months_in_range(start: datetime, end: datetime) -> list[str]:
    """Every month key whose window intersects ``[start, end)``, oldest first."""
    if end <= start:
        return [month_key(start)]
    keys: list[str] = []
    cur = month_start(month_key(start))
    while cur < end:
        keys.append(month_key(cur))
        cur = month_end(month_key(cur))
    return keys


def year_month_keys(year: int, *, not_after: datetime) -> list[str]:
    """The months of ``year`` that have already begun by ``not_after``, oldest first."""
    return [
        f"{year:04d}-{m:02d}"
        for m in range(1, 13)
        if datetime(year, m, 1, tzinfo=UTC) < not_after
    ]


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
class MonthStore:
    """One directory of ``YYYY-MM.parquet`` files for a single account."""

    def __init__(self, root: Path, account: str) -> None:
        self.dir = Path(root) / account
        self.meta_path = self.dir / "_meta.json"

    # -- presence ------------------------------------------------------- #
    def account_seen(self) -> bool:
        return self.dir.is_dir()

    def path(self, key: str) -> Path:
        return self.dir / f"{key}{_EXT}"

    def has(self, key: str) -> bool:
        return self.path(key).is_file()

    def months(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(
            p.stem for p in self.dir.glob(f"[0-9][0-9][0-9][0-9]-[0-9][0-9]{_EXT}")
        )

    # -- io ----------------------------------------------------------- #
    def load(self, key: str) -> list[Message]:
        return read_parquet(self.path(key))

    def save(self, key: str, messages: list[Message]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=f".{key}.", suffix=_EXT + ".tmp")
        os.close(fd)
        try:
            write_parquet(Path(tmp), messages)
            os.replace(tmp, self.path(key))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self._write_meta(key, len(messages))

    def clear(self) -> int:
        n = 0
        if self.dir.is_dir():
            for p in self.dir.glob("*"):
                p.unlink()
                n += 1
            self.dir.rmdir()
        return n

    # -- meta ------------------------------------------------------- #
    def meta(self) -> dict[str, dict]:
        if not self.meta_path.is_file():
            return {}
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_meta(self, key: str, count: int) -> None:
        data = self.meta()
        data[key] = {"fetched_at": datetime.now(UTC).isoformat(), "count": count}
        self.meta_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
@dataclass
class FetchReport:
    months_from_cache: list[str]
    months_fetched: list[str]
    backfilled: bool


class CachedFetcher:
    """Provider-agnostic cache in front of one :class:`ChatClient`."""

    def __init__(
        self,
        client: ChatClient,
        root: Path | str = DEFAULT_CACHE_ROOT,
        *,
        progress=None,
    ) -> None:
        self.client = client
        self.store = MonthStore(Path(root), client.name)
        self.last_report: FetchReport | None = None
        self.reporter: Reporter = as_reporter(progress)

    # ------------------------------------------------------------------ #
    def fetch(
        self,
        flt: FetchFilter | None = None,
        *,
        backfill: bool = False,
        refresh: bool = False,
        offline: bool = False,
        now: datetime | None = None,
    ) -> list[Message]:
        flt = flt or FetchFilter()
        now = now or datetime.now(UTC)
        current = month_key(now)

        if offline:
            return self._fetch_offline(flt, now)

        self.client.ensure_auth()
        channels = self.client.list_channels(flt)
        by_id: dict[str, Channel] = {c.id: c for c in channels}

        since = flt.resolved_since(now)
        first_time = not self.store.account_seen()
        # only auto-backfill a genuinely open-ended request; an unread / mentions
        # query is inherently recent and must never trigger a full history walk.
        auto = since is None and first_time and not (flt.unread_only or flt.mentions_only)
        do_backfill = backfill or auto

        from_cache: list[str] = []
        fetched: list[str] = []
        merged: dict[str, Message] = {}

        acct = self.client.name

        def take(key: str) -> bool:
            live = refresh or key == current or not self.store.has(key)
            self.reporter.month_start(acct, key, len(channels), live)
            if live:
                def on_channel(ch: Channel, i: int, n: int) -> None:
                    self.reporter.channel(acct, key, i, n, ch.name)

                msgs = list(
                    self.client.messages_between(
                        month_start(key), month_end(key), channels, on_channel=on_channel
                    )
                )
                self.store.save(key, msgs)
                fetched.append(key)
            else:
                msgs = self.store.load(key)
                from_cache.append(key)
            self.reporter.month_done(acct, key, len(msgs))
            for m in msgs:
                merged[m.id] = m
            return bool(msgs)

        if do_backfill:
            year = now.year
            while year >= _MIN_YEAR:
                got = False
                for key in year_month_keys(year, not_after=month_end(current)):
                    if take(key):
                        got = True
                if not got:
                    break                       # first fully empty year -> stop
                year -= 1
        else:
            lo = since if since is not None else month_start(current)
            hi = flt.resolved_until(now) or now
            for key in months_in_range(lo, hi):
                take(key)

        self.reporter.close()
        self.last_report = FetchReport(from_cache, fetched, do_backfill)

        out: list[Message] = []
        for m in merged.values():
            ch = by_id.get(m.channel_id)
            _recompute_unread(m, ch)
            if ch is not None and not flt.wants_channel(ch, use_unread_count=False):
                continue
            if ch is None and flt.channels:
                continue
            if flt.accepts_message(m, now):
                out.append(m)
        out = cap_per_channel(out, flt.limit_per_channel)
        out.sort(key=Message.sort_key)
        return out

    # ------------------------------------------------------------------ #
    def _fetch_offline(self, flt: FetchFilter, now: datetime) -> list[Message]:
        """Serve purely from disk — no auth, no network, no current-month refetch.

        ``is_unread`` keeps whatever value was stored when the month was cached.
        """
        present = self.store.months()
        since = flt.resolved_since(now)
        until = flt.resolved_until(now) or now
        if since is not None:
            wanted = set(months_in_range(month_start(month_key(since)), until))
            keys = [k for k in present if k in wanted]
        else:
            keys = present

        merged: dict[str, Message] = {}
        for key in keys:
            for m in self.store.load(key):
                merged[m.id] = m
        self.last_report = FetchReport(list(keys), [], backfilled=False)

        # channel stubs reconstructed from the messages themselves
        stubs: dict[str, Channel] = {}
        for m in merged.values():
            stubs.setdefault(
                m.channel_id,
                Channel(
                    id=m.channel_id, name=m.channel_name, kind=m.channel_kind,
                    account=m.account, provider=m.provider,
                ),
            )

        out: list[Message] = []
        for m in merged.values():
            ch = stubs.get(m.channel_id)
            if ch is not None and not flt.wants_channel(ch, use_unread_count=False):
                continue
            if flt.accepts_message(m, now):
                out.append(m)
        out = cap_per_channel(out, flt.limit_per_channel)
        out.sort(key=Message.sort_key)
        return out


def _recompute_unread(m: Message, channel: Channel | None) -> None:
    """Refresh ``is_unread`` from the live read marker; leave text-derived flags."""
    if channel is None or channel.last_read_at is None:
        m.is_unread = False
        return
    m.is_unread = (not m.is_own) and m.timestamp > channel.last_read_at
