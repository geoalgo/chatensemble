from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

from chatensemble.base import ChatClient
from chatensemble.cache import (
    CachedFetcher,
    MonthStore,
    month_end,
    month_key,
    month_start,
    months_in_range,
    year_month_keys,
)
from chatensemble.config import AccountConfig
from chatensemble.filters import FetchFilter
from chatensemble.models import Channel, ChannelKind, Message

UTC = timezone.utc
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def mk_msg(mid: str, when: datetime, *, channel: str = "c1", own: bool = False) -> Message:
    return Message(
        id=mid,
        text=f"msg {mid}",
        timestamp=when,
        author_id="me" if own else "u1",
        author_name="me" if own else "alice",
        channel_id=channel,
        channel_name=channel,
        channel_kind=ChannelKind.PUBLIC,
        account="fake",
        provider="fake",
        is_own=own,
    )


class FakeClient(ChatClient):
    provider = "fake"

    def __init__(self, messages: list[Message], channels: list[Channel]) -> None:
        super().__init__(AccountConfig(name="fake", type="mattermost", settings={}))
        self._messages = messages
        self._channels = channels
        self.windows: list[tuple[str, datetime, datetime]] = []

    def _build_http(self):  # pragma: no cover - unused
        raise AssertionError("network must not be touched")

    def authenticate(self) -> None:
        pass

    def list_channels(self, flt: FetchFilter | None = None) -> list[Channel]:
        return list(self._channels)

    def _iter_channel_between(
        self, channel: Channel, start: datetime, end: datetime
    ) -> Iterator[Message]:
        self.windows.append((channel.id, start, end))
        for m in self._messages:
            if m.channel_id == channel.id and start <= m.timestamp < end:
                yield m

    # convenience
    def starts(self) -> set[datetime]:
        return {s for _, s, _ in self.windows}


# --------------------------------------------------------------------------- #
# month arithmetic
# --------------------------------------------------------------------------- #
def test_month_helpers():
    assert month_key(datetime(2026, 9, 3, tzinfo=UTC)) == "2026-09"
    assert month_start("2026-09") == datetime(2026, 9, 1, tzinfo=UTC)
    assert month_end("2026-12") == datetime(2027, 1, 1, tzinfo=UTC)
    assert months_in_range(
        datetime(2026, 7, 15, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)
    ) == ["2026-07", "2026-08", "2026-09"]
    assert year_month_keys(2026, not_after=NOW) == [f"2026-{m:02d}" for m in range(1, 10)]
    assert year_month_keys(2025, not_after=NOW) == [f"2025-{m:02d}" for m in range(1, 13)]


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def test_store_roundtrip(tmp_path):
    store = MonthStore(tmp_path, "acc")
    msgs = [mk_msg("1", datetime(2026, 8, 4, tzinfo=UTC))]
    msgs[0].reactions = {"tada": 2}
    msgs[0].raw = {"nested": {"x": 1}}

    store.save("2026-08", msgs)
    assert store.has("2026-08")
    assert store.months() == ["2026-08"]

    loaded = store.load("2026-08")
    assert len(loaded) == 1
    got = loaded[0]
    assert got.id == "1"
    assert got.timestamp == msgs[0].timestamp
    assert got.channel_kind == ChannelKind.PUBLIC
    assert got.reactions == {"tada": 2}
    assert got.raw == {"nested": {"x": 1}}
    assert store.meta()["2026-08"]["count"] == 1


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def _chan(last_read: datetime | None) -> Channel:
    return Channel(id="c1", name="c1", kind=ChannelKind.PUBLIC, account="fake",
                   provider="fake", last_read_at=last_read)


def test_first_fetch_backfills_until_empty_year(tmp_path):
    msgs = [
        mk_msg("a", datetime(2026, 8, 10, tzinfo=UTC)),
        mk_msg("b", datetime(2026, 9, 2, tzinfo=UTC)),
    ]
    client = FakeClient(msgs, [_chan(None)])
    cf = CachedFetcher(client, tmp_path)

    out = cf.fetch(FetchFilter(), now=NOW)

    assert cf.last_report.backfilled is True
    assert {m.id for m in out} == {"a", "b"}
    # 2026 (has data) and 2025 (empty) written; 2024 never touched
    assert cf.store.has("2026-08") and cf.store.has("2026-09")
    assert cf.store.has("2025-06")
    assert cf.store.load("2025-06") == []
    assert not cf.store.has("2024-12")


def test_second_fetch_covers_only_current_month(tmp_path):
    msgs = [
        mk_msg("a", datetime(2026, 8, 10, tzinfo=UTC)),
        mk_msg("b", datetime(2026, 9, 2, tzinfo=UTC)),
    ]
    client = FakeClient(msgs, [_chan(None)])
    cf = CachedFetcher(client, tmp_path)
    cf.fetch(FetchFilter(), now=NOW)                      # first run: backfill

    client.windows.clear()
    out = cf.fetch(FetchFilter(), now=NOW)                # steady state

    assert cf.last_report.backfilled is False
    assert client.starts() == {month_start("2026-09")}    # only current month hit
    assert {m.id for m in out} == {"b"}                   # August not in range anymore


def test_since_serves_past_months_from_cache(tmp_path):
    msgs = [
        mk_msg("a", datetime(2026, 8, 10, tzinfo=UTC)),
        mk_msg("b", datetime(2026, 9, 2, tzinfo=UTC)),
    ]
    client = FakeClient(msgs, [_chan(None)])
    cf = CachedFetcher(client, tmp_path)
    cf.fetch(FetchFilter(), now=NOW)

    client.windows.clear()
    out = cf.fetch(FetchFilter(since="2026-07-01"), now=NOW)

    assert client.starts() == {month_start("2026-09")}    # only current re-fetched
    assert {m.id for m in out} == {"a", "b"}              # Aug from disk, Sep live


def test_refresh_refetches_every_month_in_range(tmp_path):
    msgs = [
        mk_msg("a", datetime(2026, 8, 10, tzinfo=UTC)),
        mk_msg("b", datetime(2026, 9, 2, tzinfo=UTC)),
    ]
    client = FakeClient(msgs, [_chan(None)])
    cf = CachedFetcher(client, tmp_path)
    cf.fetch(FetchFilter(), now=NOW)

    client.windows.clear()
    cf.fetch(FetchFilter(since="2026-08-01"), now=NOW, refresh=True)
    assert client.starts() == {month_start("2026-08"), month_start("2026-09")}


def test_explicit_backfill_after_first_run(tmp_path):
    client = FakeClient([mk_msg("b", datetime(2026, 9, 2, tzinfo=UTC))], [_chan(None)])
    cf = CachedFetcher(client, tmp_path)
    cf.fetch(FetchFilter(), now=NOW)

    client.windows.clear()
    cf.fetch(FetchFilter(), now=NOW, backfill=True)
    assert cf.last_report.backfilled is True


def test_offline_serves_only_cached_months_without_network(tmp_path):
    msgs = [
        mk_msg("a", datetime(2026, 8, 10, tzinfo=UTC)),
        mk_msg("b", datetime(2026, 9, 2, tzinfo=UTC)),
    ]
    client = FakeClient(msgs, [_chan(None)])
    cf = CachedFetcher(client, tmp_path)
    cf.fetch(FetchFilter(), now=NOW)                      # populate via backfill

    client.windows.clear()
    out = cf.fetch(FetchFilter(), now=NOW, offline=True)

    assert client.windows == []                           # nothing fetched
    assert cf.last_report.months_fetched == []
    assert {m.id for m in out} == {"a", "b"}              # every cached month


def test_offline_with_empty_cache_returns_nothing(tmp_path):
    client = FakeClient([mk_msg("b", datetime(2026, 9, 2, tzinfo=UTC))], [_chan(None)])
    cf = CachedFetcher(client, tmp_path)

    out = cf.fetch(FetchFilter(), now=NOW, offline=True)
    assert out == []
    assert client.windows == []


def test_offline_respects_since(tmp_path):
    msgs = [
        mk_msg("a", datetime(2026, 8, 10, tzinfo=UTC)),
        mk_msg("b", datetime(2026, 9, 2, tzinfo=UTC)),
    ]
    client = FakeClient(msgs, [_chan(None)])
    cf = CachedFetcher(client, tmp_path)
    cf.fetch(FetchFilter(), now=NOW)

    client.windows.clear()
    out = cf.fetch(FetchFilter(since="2026-09-01"), now=NOW, offline=True)
    assert client.windows == []
    assert {m.id for m in out} == {"b"}


def test_unread_is_recomputed_from_live_marker(tmp_path):
    m = mk_msg("b", datetime(2026, 9, 2, tzinfo=UTC))
    m.is_unread = True                                    # stale value on disk
    chan = _chan(datetime(2026, 9, 3, tzinfo=UTC))        # read marker AFTER the message
    client = FakeClient([m], [chan])
    cf = CachedFetcher(client, tmp_path)

    out = cf.fetch(FetchFilter(), now=NOW)
    assert out[0].is_unread is False

    chan.last_read_at = datetime(2026, 9, 1, tzinfo=UTC)  # now BEFORE the message
    out = cf.fetch(FetchFilter(unread_only=True), now=NOW)
    assert [x.id for x in out] == ["b"]
