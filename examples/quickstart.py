"""Minimal end-to-end example.

    uv run python examples/quickstart.py
"""

from datetime import datetime, timedelta, timezone

from unichat import (
    ChatManager,
    FetchFilter,
    print_channels,
    print_feed,
    print_messages,
)

with ChatManager.from_dir() as mgr:          # ~/unichat/accounts by default
    # 1. what accounts are configured
    print("accounts:", [c.name for c in mgr.clients])

    # 2. channels + unread counts across every account
    print_channels(mgr.channels())

    # 3. unread messages from the last two weeks
    since = datetime.now(timezone.utc) - timedelta(days=14)
    unread = mgr.fetch(FetchFilter(since=since, unread_only=True))
    print_messages(unread, title="Unread (14d)")            # table
    print_feed(unread, title="Unread (14d)", width=90)      # feed of cards

    # 4. anything mentioning a keyword, wrapped for reading
    hits = mgr.fetch(FetchFilter(since="30d", query=r"deadline|urgent|release"))
    print_messages(hits, title="Keyword hits", wrap=True, width=90)

    if mgr.errors:
        print("per-account errors:", mgr.errors)
