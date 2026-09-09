"""ChatManager: load accounts and fetch across all of them."""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import ChatClient, ChatClientError
from .cache import DEFAULT_CACHE_ROOT, CachedFetcher, MonthStore
from .config import AccountConfig, DEFAULT_ACCOUNTS_DIR, load_accounts
from .filters import FetchFilter
from .models import Channel, Message
from .providers import create_client


class ChatManager:
    """Aggregate view over every configured account."""

    def __init__(
        self,
        accounts: Iterable[AccountConfig],
        *,
        cache_root: str | Path = DEFAULT_CACHE_ROOT,
    ) -> None:
        self.clients: list[ChatClient] = [create_client(a) for a in accounts]
        self.cache_root = Path(cache_root)
        self.errors: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    @classmethod
    def from_dir(
        cls,
        directory: str | Path = DEFAULT_ACCOUNTS_DIR,
        *,
        cache_root: str | Path = DEFAULT_CACHE_ROOT,
    ) -> "ChatManager":
        return cls(load_accounts(directory), cache_root=cache_root)

    def client(self, name: str) -> ChatClient:
        for c in self.clients:
            if c.name == name:
                return c
        raise KeyError(f"no account named {name!r}")

    def _selected(self, flt: FetchFilter) -> list[ChatClient]:
        return [c for c in self.clients if flt.wants_account(c.name)]

    def mark_read(
        self, account: str, channel_id: str, *, up_to: datetime | None = None
    ) -> None:
        """Advance ``account``'s server-side read marker for ``channel_id``."""
        self.client(account).mark_read(channel_id, up_to=up_to)

    # ------------------------------------------------------------------ #
    def channels(self, flt: FetchFilter | None = None) -> list[Channel]:
        flt = flt or FetchFilter()
        out: list[Channel] = []
        for c in self._selected(flt):
            c.ensure_auth()
            out.extend(c.list_channels(flt))
        return out

    def fetch(
        self,
        flt: FetchFilter | None = None,
        *,
        cache: bool = True,
        backfill: bool = False,
        refresh: bool = False,
        offline: bool = False,
        progress: Any = None,
        max_workers: int | None = None,
    ) -> list[Message]:
        """Fetch messages across all selected accounts, merged and time-sorted.

        With ``cache=True`` (default) each account is served through a
        month-partitioned on-disk cache: past months come from disk, the current
        month is always re-fetched. ``backfill=True`` walks history back a year
        at a time until an empty year; ``refresh=True`` re-fetches every month in
        range; ``offline=True`` serves purely from disk with no network at all
        (implies ``cache``). ``progress`` may be ``True`` (a tqdm bar), a
        :class:`~unichat.progress.Reporter`, or a ``callable(str)``.
        Errors from one account are recorded in :attr:`errors` and skipped.

        Accounts are fetched concurrently (one thread each, up to ``max_workers``
        -- default ``min(#accounts, 8)``); each account keeps its own client,
        cache directory and rate limits, so they don't contend.
        """
        flt = flt or FetchFilter()
        if offline:
            cache = True
        self.errors = {}
        clients = self._selected(flt)
        multi = len(clients) > 1

        def run_one(item: tuple[int, ChatClient]):
            idx, c = item
            rep = progress
            if multi and progress is True:               # give each account its own bar row
                from .progress import TqdmReporter

                rep = TqdmReporter(position=idx, leave=False)
            try:
                if cache:
                    cf = CachedFetcher(c, self.cache_root, progress=rep)
                    msgs = cf.fetch(flt, backfill=backfill, refresh=refresh, offline=offline)
                else:
                    msgs = c.fetch(flt)
                return c.name, msgs, None
            except ChatClientError as exc:
                return c.name, [], str(exc)

        if multi:
            with ThreadPoolExecutor(max_workers=max_workers or min(len(clients), 8)) as pool:
                results = list(pool.map(run_one, enumerate(clients)))
        else:
            results = [run_one((0, c)) for c in clients]

        messages: list[Message] = []
        for name, msgs, err in results:
            if err is not None:
                self.errors[name] = err
            else:
                messages.extend(msgs)
        messages.sort(key=Message.sort_key)
        if flt.max_messages:
            messages = messages[-flt.max_messages :]
        return messages

    def unread(self, flt: FetchFilter | None = None, **overrides) -> list[Message]:
        base = flt or FetchFilter()
        base.unread_only = True
        for k, v in overrides.items():
            setattr(base, k, v)
        if base.since is None:
            base.since = "90d"          # unread is recent; keep the scan bounded
        return self.fetch(base)

    # ------------------------------------------------------------------ #
    def cache_status(self) -> dict[str, dict]:
        """``{account: {month: count}}`` for every cached month on disk."""
        status: dict[str, dict] = {}
        for c in self.clients:
            store = MonthStore(self.cache_root, c.name)
            meta = store.meta()
            status[c.name] = {
                m: meta.get(m, {}).get("count") for m in store.months()
            }
        return status

    def cache_clear(self, account: str | None = None) -> int:
        removed = 0
        for c in self.clients:
            if account and c.name != account:
                continue
            removed += MonthStore(self.cache_root, c.name).clear()
        return removed

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        for c in self.clients:
            c.close()

    def __enter__(self):  # pragma: no cover - trivial
        return self

    def __exit__(self, *exc):  # pragma: no cover - trivial
        self.close()
