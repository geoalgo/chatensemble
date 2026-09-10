"""Provider-agnostic client interface.

Providers implement exactly two data operations:

* :meth:`ChatClient.list_channels` — the visible channels, with fresh read markers.
* :meth:`ChatClient._iter_channel_between` — messages in one channel within a
  half-open ``[start, end)`` time window.

Everything provider-specific (pagination, rate limits, a missing server-side upper
bound, cursor resume) lives behind those. :class:`~chatensemble.filters.FetchFilter`
never reaches a provider.
"""

from __future__ import annotations

import abc
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta, timezone

import httpx

from .config import AccountConfig
from .filters import FetchFilter, cap_per_channel
from .models import Channel, Message


class ChatClientError(RuntimeError):
    """Raised for auth failures and unrecoverable API errors."""


class ChatClient(abc.ABC):
    """One authenticated connection to a chat service."""

    provider: str = "base"
    #: window used by :meth:`fetch` when the filter carries no lower bound
    default_lookback_days: int = 30

    def __init__(self, account: AccountConfig) -> None:
        self.account = account
        self.name = account.name
        self._http: httpx.Client | None = None
        self._authed = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = self._build_http()
        return self._http

    @abc.abstractmethod
    def _build_http(self) -> httpx.Client:
        ...

    @abc.abstractmethod
    def authenticate(self) -> None:
        """Perform login / validate the token. Idempotent."""

    def ensure_auth(self) -> None:
        if not self._authed:
            self.authenticate()
            self._authed = True

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self):  # pragma: no cover - trivial
        self.ensure_auth()
        return self

    def __exit__(self, *exc):  # pragma: no cover - trivial
        self.close()

    # ------------------------------------------------------------------ #
    # provider hooks
    # ------------------------------------------------------------------ #
    @abc.abstractmethod
    def list_channels(self, flt: FetchFilter | None = None) -> list[Channel]:
        """Return channels visible to the account (unread markers populated)."""

    @abc.abstractmethod
    def _iter_channel_between(
        self, channel: Channel, start: datetime, end: datetime
    ) -> Iterator[Message]:
        """Yield messages in ``channel`` with ``start <= ts < end``.

        Order is not guaranteed. The provider is responsible for paging,
        rate-limiting and discarding anything outside the window.
        """

    def mark_read(self, channel_id: str, *, up_to: datetime | None = None) -> None:
        """Advance this account's read marker for ``channel_id`` on the server.

        ``up_to`` is the point to mark read to (default: the channel's latest
        message). Providers whose API only exposes a channel-level marker ignore
        ``up_to`` and mark the whole channel read. Raises
        :class:`NotImplementedError` when the provider has no such API.
        """
        raise NotImplementedError(f"{self.provider} does not support mark_read")

    # ------------------------------------------------------------------ #
    # provider-agnostic API
    # ------------------------------------------------------------------ #
    def messages_between(
        self,
        start: datetime,
        end: datetime,
        channels: Sequence[Channel] | None = None,
        *,
        on_channel: Callable[[Channel, int, int], None] | None = None,
    ) -> Iterator[Message]:
        """Every message across ``channels`` (default: all) within ``[start, end)``.

        ``on_channel(channel, index, total)`` is called before each channel is
        scanned, for progress reporting.
        """
        self.ensure_auth()
        if channels is None:
            channels = self.list_channels()
        total = len(channels)
        for i, ch in enumerate(channels, 1):
            if on_channel is not None:
                on_channel(ch, i, total)
            yield from self._iter_channel_between(ch, start, end)

    def fetch(self, flt: FetchFilter | None = None) -> list[Message]:
        """One-shot fetch honouring ``flt``; no caching (see ChatManager for that)."""
        flt = flt or FetchFilter()
        self.ensure_auth()
        now = datetime.now(timezone.utc)
        start = flt.resolved_since(now) or (now - timedelta(days=self.default_lookback_days))
        end = flt.resolved_until(now) or now

        channels = [c for c in self.list_channels(flt) if flt.wants_channel(c)]
        out = [
            m
            for m in self.messages_between(start, end, channels)
            if flt.accepts_message(m, now)
        ]
        out = cap_per_channel(out, flt.limit_per_channel)
        out.sort(key=Message.sort_key)
        return out
