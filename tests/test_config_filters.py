from datetime import datetime, timezone

import pytest

from chatensemble.config import ConfigError, load_account_file, load_accounts
from chatensemble.filters import FetchFilter, parse_when
from chatensemble.models import Channel, ChannelKind, Message

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("7d", datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)),
        ("24h", datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)),
        ("90m", datetime(2026, 8, 31, 10, 30, tzinfo=timezone.utc)),
        ("2w", datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)),
        ("2026-08-01", datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)),
    ],
)
def test_parse_when(expr, expected):
    assert parse_when(expr, now=NOW) == expected


def test_parse_when_none_and_datetime():
    assert parse_when(None) is None
    naive = datetime(2026, 1, 1, 0, 0)
    assert parse_when(naive).tzinfo is timezone.utc


def _msg(**kw):
    base = dict(
        id="1", text="hello world", timestamp=NOW, author_id="u1", author_name="alice",
        channel_id="c1", channel_name="general", channel_kind=ChannelKind.PUBLIC,
        account="acc", provider="mattermost",
    )
    base.update(kw)
    return Message(**base)


def test_accepts_message_time_bounds():
    flt = FetchFilter(since="1d", until="now")
    assert flt.accepts_message(_msg(timestamp=NOW), now=NOW)
    old = _msg(timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert not flt.accepts_message(old, now=NOW)


def test_accepts_message_query_and_unread_and_own():
    assert FetchFilter(query="wor.d").accepts_message(_msg())
    assert not FetchFilter(query="nope").accepts_message(_msg())
    assert not FetchFilter(unread_only=True).accepts_message(_msg(is_unread=False))
    assert not FetchFilter(include_own=False).accepts_message(_msg(is_own=True))
    assert not FetchFilter(include_threads=False).accepts_message(_msg(id="2", thread_id="1"))


def test_wants_channel_globs_and_dms():
    ch = Channel(id="c1", name="eng-backend", kind=ChannelKind.PUBLIC)
    assert FetchFilter(channels=["eng-*"]).wants_channel(ch)
    assert not FetchFilter(channels=["ops-*"]).wants_channel(ch)
    dm = Channel(id="d1", name="bob", kind=ChannelKind.DIRECT)
    assert not FetchFilter(include_dms=False).wants_channel(dm)


def test_load_account_file_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_PW", "s3cr3t")
    p = tmp_path / "work.yaml"
    p.write_text("type: mattermost\nserver: https://mm.example\nusername: a\npassword: ${MM_PW}\n")
    acc = load_account_file(p)
    assert acc.type == "mattermost"
    assert acc.name == "work"
    assert acc.get("password") == "s3cr3t"


def test_load_account_type_inference(tmp_path):
    p = tmp_path / "slack-team.yaml"
    p.write_text("token: xoxp-abc\n")
    assert load_account_file(p).type == "slack"


def test_load_accounts_skips_example(tmp_path):
    (tmp_path / "example.yaml").write_text("type: slack\ntoken: xoxp-x\n")
    (tmp_path / "real.yaml").write_text("type: slack\ntoken: xoxp-y\n")
    accs = load_accounts(tmp_path)
    assert [a.name for a in accs] == ["real"]


def test_missing_required_field(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("type: mattermost\nserver: https://x\nusername: a\n")
    acc = load_account_file(p)
    with pytest.raises(ConfigError):
        acc.get("password", required=True)
