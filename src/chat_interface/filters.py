"""Filter options for :func:`fetch`."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dateutil import parser as _dateparser

from .models import Channel, Message, to_utc

_REL_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_REL_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def parse_when(value: str | datetime | None, *, now: datetime | None = None) -> datetime | None:
    """Parse a date/time expression into a tz-aware UTC datetime.

    Accepts ``datetime`` objects, ISO-8601 strings ("2026-08-01",
    "2026-08-01T12:00:00Z"), and relative offsets into the past such as
    ``"7d"``, ``"24h"``, ``"90m"``, ``"2w"``.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_utc(value)

    now = now or datetime.now(timezone.utc)
    text = value.strip()
    m = _REL_RE.match(text)
    if m:
        qty, unit = int(m.group(1)), m.group(2).lower()
        return now - timedelta(**{_REL_UNITS[unit]: qty})
    if text.lower() in {"now", "today"}:
        base = now if text.lower() == "now" else now.replace(hour=0, minute=0, second=0, microsecond=0)
        return base
    if text.lower() == "yesterday":
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return to_utc(_dateparser.parse(text))


@dataclass
class FetchFilter:
    """Options controlling which messages :func:`fetch` returns.

    All fields are optional. ``since`` / ``until`` accept anything
    :func:`parse_when` understands.
    """

    since: str | datetime | None = None
    until: str | datetime | None = None
    channels: list[str] | None = None          # glob patterns matched against channel name or id
    accounts: list[str] | None = None          # restrict to these account names
    unread_only: bool = False
    mentions_only: bool = False
    query: str | None = None                   # regex, matched against message text (case-insensitive)
    include_dms: bool = True
    include_threads: bool = True
    include_own: bool = True                   # include messages you sent
    # Per-channel hard cap on messages pulled from the API (newest first).
    limit_per_channel: int = 500
    # Overall cap on the merged result (0 = unlimited).
    max_messages: int = 0

    # ------------------------------------------------------------------ #
    # resolved helpers
    # ------------------------------------------------------------------ #
    def resolved_since(self, now: datetime | None = None) -> datetime | None:
        return parse_when(self.since, now=now)

    def resolved_until(self, now: datetime | None = None) -> datetime | None:
        return parse_when(self.until, now=now)

    @property
    def _query_re(self) -> re.Pattern[str] | None:
        if not self.query:
            return None
        return re.compile(self.query, re.IGNORECASE)

    def wants_account(self, name: str) -> bool:
        return not self.accounts or name in self.accounts

    def wants_channel(self, ch: Channel, *, use_unread_count: bool = True) -> bool:
        """Whether ``ch`` should be visited.

        ``use_unread_count=False`` skips the channel-level unread short-circuit —
        used by the cache path, where per-message ``is_unread`` is recomputed
        from fresh read markers and is the source of truth.
        """
        if not self.include_dms and ch.kind.value in {"direct", "group"}:
            return False
        if use_unread_count and self.unread_only and not ch.has_unread:
            return False
        if not self.channels:
            return True
        return any(
            fnmatch.fnmatch(ch.name.lower(), pat.lower()) or fnmatch.fnmatch(ch.id, pat)
            for pat in self.channels
        )

    def accepts_message(self, msg: Message, now: datetime | None = None) -> bool:
        since = self.resolved_since(now)
        until = self.resolved_until(now)
        if since and msg.timestamp < since:
            return False
        if until and msg.timestamp > until:
            return False
        if self.unread_only and not msg.is_unread:
            return False
        if self.mentions_only and not msg.is_mention:
            return False
        if not self.include_threads and msg.is_thread_reply:
            return False
        if not self.include_own and msg.is_own:
            return False
        rx = self._query_re
        if rx and not rx.search(msg.text or ""):
            return False
        return True


def cap_per_channel(messages: Sequence[Message], limit: int) -> list[Message]:
    """Keep only the ``limit`` newest messages per channel (0 = keep all)."""
    if not limit:
        return list(messages)
    seen: dict[str, int] = {}
    kept: list[Message] = []
    for m in sorted(messages, key=Message.sort_key, reverse=True):
        n = seen.get(m.channel_id, 0)
        if n < limit:
            kept.append(m)
            seen[m.channel_id] = n + 1
    kept.sort(key=Message.sort_key)
    return kept
