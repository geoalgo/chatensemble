"""Core data models shared across providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ChannelKind(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"
    DIRECT = "direct"
    GROUP = "group"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class Channel:
    """A conversation container (channel, DM, group DM)."""

    id: str
    name: str
    kind: ChannelKind = ChannelKind.UNKNOWN
    account: str = ""
    provider: str = ""
    # team / workspace scope, when the provider has one (Mattermost teams)
    team_id: str | None = None
    team_name: str | None = None
    # unread bookkeeping, filled in lazily by the provider
    unread_count: int = 0
    mention_count: int = 0
    last_read_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def has_unread(self) -> bool:
        return self.unread_count > 0

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        prefix = {
            ChannelKind.PUBLIC: "#",
            ChannelKind.PRIVATE: "🔒",
            ChannelKind.DIRECT: "@",
            ChannelKind.GROUP: "👥",
        }.get(self.kind, "")
        return f"{prefix}{self.name}"


@dataclass(slots=True)
class Message:
    """A single chat message, normalised across providers."""

    id: str
    text: str
    timestamp: datetime  # always tz-aware, UTC
    author_id: str
    author_name: str
    channel_id: str
    channel_name: str
    channel_kind: ChannelKind = ChannelKind.UNKNOWN
    account: str = ""
    provider: str = ""
    thread_id: str | None = None
    reply_count: int = 0
    is_unread: bool = False
    is_mention: bool = False
    is_own: bool = False
    edited: bool = False
    permalink: str | None = None
    reactions: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_thread_reply(self) -> bool:
        return self.thread_id is not None and self.thread_id != self.id

    def sort_key(self) -> tuple[datetime, str]:
        return (self.timestamp, self.id)


def to_utc(dt: datetime) -> datetime:
    """Normalise any datetime to a tz-aware UTC datetime."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def from_epoch_ms(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def from_epoch_s(s: int | float) -> datetime:
    return datetime.fromtimestamp(float(s), tz=timezone.utc)
