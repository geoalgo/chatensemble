"""unichat: unified fetch/visualise for Mattermost, Slack, Discord."""

from __future__ import annotations

from .base import ChatClient, ChatClientError
from .cache import CachedFetcher, MonthStore, months_in_range
from .config import AccountConfig, ConfigError, load_accounts
from .display import (
    build_feed,
    build_table,
    make_console,
    print_channels,
    print_feed,
    print_messages,
)
from .browser import ThreadBrowser, browse_threads
from .filters import FetchFilter, parse_when
from .manager import ChatManager
from .models import Channel, ChannelKind, Message
from .progress import CallbackReporter, ChannelProgressReporter, Reporter, TqdmReporter
from .providers import create_client
from .synthetic import DEFAULT_ACCOUNTS, PERSONAS, synthetic_messages
from .threads import Thread, group_threads

__version__ = "0.1.0"

__all__ = [
    "ChatManager",
    "FetchFilter",
    "parse_when",
    "Message",
    "Channel",
    "ChannelKind",
    "ChatClient",
    "ChatClientError",
    "AccountConfig",
    "ConfigError",
    "load_accounts",
    "create_client",
    "CachedFetcher",
    "MonthStore",
    "months_in_range",
    "Reporter",
    "TqdmReporter",
    "CallbackReporter",
    "ChannelProgressReporter",
    "print_messages",
    "print_feed",
    "print_channels",
    "build_table",
    "build_feed",
    "make_console",
    "Thread",
    "group_threads",
    "synthetic_messages",
    "PERSONAS",
    "DEFAULT_ACCOUNTS",
    "browse_threads",
    "ThreadBrowser",
]
