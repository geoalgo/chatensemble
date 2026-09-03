"""Provider registry."""

from __future__ import annotations

from ..base import ChatClient
from ..config import AccountConfig
from .mattermost import MattermostClient
from .slack import SlackClient

_REGISTRY: dict[str, type[ChatClient]] = {
    "mattermost": MattermostClient,
    "slack": SlackClient,
}


def create_client(account: AccountConfig) -> ChatClient:
    try:
        cls = _REGISTRY[account.type]
    except KeyError:
        raise ValueError(
            f"no provider registered for account type {account.type!r} "
            f"(have: {', '.join(sorted(_REGISTRY))})"
        ) from None
    return cls(account)


__all__ = ["create_client", "MattermostClient", "SlackClient"]
