"""Group flat :class:`Message` lists into threads.

A thread is a root message plus every reply that carries its ``thread_id``.
This is provider-agnostic: it works on anything ``fetch`` returns as well as on
the synthetic data in :mod:`unichat.synthetic`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Message

# Provider "system" posts (joins, leaves, header edits, pins). They carry no
# conversational content and only clutter a thread list.
_SYSTEM_RE = re.compile(
    r"\b(joined|left|added to|removed from|was added to|was removed from) "
    r"the (channel|team|conversation|workspace)\b"
    r"|\b(updated|set|removed|changed) the channel (header|purpose|display name|name|icon)\b"
    r"|\brenamed the channel\b"
    r"|\bconverted the channel\b"
    r"|\b(pinned|un-?pinned) (a|this) message\b",
    re.IGNORECASE,
)


def is_system_message(m: Message) -> bool:
    """True for empty posts and provider join/leave/header notices."""
    text = (m.text or "").strip()
    return not text or bool(_SYSTEM_RE.search(text))


@dataclass(slots=True)
class Thread:
    """A root message and its replies, in time order."""

    root: Message
    replies: list[Message] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.root.thread_id or self.root.id

    @property
    def account(self) -> str:
        return self.root.account

    @property
    def provider(self) -> str:
        return self.root.provider

    @property
    def channel_name(self) -> str:
        return self.root.channel_name

    @property
    def reply_count(self) -> int:
        return len(self.replies)

    @property
    def messages(self) -> list[Message]:
        return [self.root, *self.replies]

    @property
    def last_activity(self):
        return self.replies[-1].timestamp if self.replies else self.root.timestamp

    @property
    def is_unread(self) -> bool:
        return any(m.is_unread for m in self.messages)

    @property
    def unread_count(self) -> int:
        return sum(1 for m in self.messages if m.is_unread)

    @property
    def is_mention(self) -> bool:
        return any(m.is_mention for m in self.messages)

    @property
    def participants(self) -> list[str]:
        seen: dict[str, None] = {}
        for m in self.messages:
            seen.setdefault(m.author_name or "(unknown)", None)
        return list(seen)


def group_threads(
    messages: list[Message], *, newest_first: bool = True, drop_system: bool = True
) -> list[Thread]:
    """Bucket ``messages`` by ``(account, thread)`` and return :class:`Thread` objects.

    The root is the message whose ``id`` equals the thread id, else the earliest
    message in the bucket. Threads are ordered by last activity. With
    ``drop_system`` (default) join/leave/header notices are filtered out first.
    """
    buckets: dict[tuple[str, str], list[Message]] = {}
    for m in messages:
        if drop_system and is_system_message(m):
            continue
        key = (m.account, m.thread_id or m.id)
        buckets.setdefault(key, []).append(m)

    threads: list[Thread] = []
    for (_, thread_id), msgs in buckets.items():
        msgs.sort(key=Message.sort_key)
        root = next((m for m in msgs if m.id == thread_id), msgs[0])
        replies = [m for m in msgs if m.id != root.id]
        threads.append(Thread(root=root, replies=replies))

    threads.sort(key=lambda t: t.last_activity, reverse=newest_first)
    return threads
