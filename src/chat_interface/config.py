"""Account configuration: one YAML file per account under ``~/unichat/accounts/``."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# credentials live outside any code checkout; override per-run with --accounts-dir
DEFAULT_ACCOUNTS_DIR = Path.home() / "unichat" / "accounts"

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}|\$([A-Z0-9_]+)")


class ConfigError(RuntimeError):
    pass


def _expand_env(value: Any) -> Any:
    """Expand ``${VAR}`` / ``$VAR`` references inside strings."""
    if not isinstance(value, str):
        return value

    def repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        if name not in os.environ:
            raise ConfigError(f"environment variable {name!r} referenced in config is not set")
        return os.environ[name]

    return _ENV_RE.sub(repl, value)


@dataclass
class AccountConfig:
    name: str
    type: str  # "mattermost" | "slack" | "discord"
    settings: dict[str, Any] = field(default_factory=dict)
    source: Path | None = None

    def get(self, key: str, default: Any = None, *, required: bool = False) -> Any:
        if key not in self.settings or self.settings[key] in (None, ""):
            if required:
                raise ConfigError(
                    f"account {self.name!r} ({self.source}): missing required field {key!r}"
                )
            return default
        return _expand_env(self.settings[key])

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        redacted = {
            k: ("***" if k in {"password", "token", "cookie", "secret"} else v)
            for k, v in self.settings.items()
        }
        return f"AccountConfig(name={self.name!r}, type={self.type!r}, settings={redacted})"


def load_account_file(path: Path) -> AccountConfig:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    acc_type = (data.pop("type", None) or _infer_type(path, data) or "").lower()
    if acc_type not in {"mattermost", "slack", "discord"}:
        raise ConfigError(
            f"{path}: unknown or missing account 'type' (got {acc_type!r}); "
            "expected one of mattermost, slack, discord"
        )
    name = str(data.pop("name", None) or path.stem)
    return AccountConfig(name=name, type=acc_type, settings=data, source=path)


def _infer_type(path: Path, data: dict[str, Any]) -> str | None:
    stem = path.stem.lower()
    for t in ("mattermost", "slack", "discord"):
        if t in stem:
            return t
    token = str(data.get("token", ""))
    if token.startswith(("xoxp-", "xoxb-", "xoxc-")):
        return "slack"
    if "server" in data or "url" in data:
        return "mattermost"
    return None


def load_accounts(directory: str | Path = DEFAULT_ACCOUNTS_DIR) -> list[AccountConfig]:
    directory = Path(directory)
    if not directory.is_dir():
        raise ConfigError(f"accounts directory {directory!r} does not exist")
    accounts: list[AccountConfig] = []
    for path in sorted(directory.glob("*.y*ml")):
        if path.name.startswith((".", "_")) or path.stem == "example":
            continue
        accounts.append(load_account_file(path))
    return accounts
