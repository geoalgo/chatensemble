"""`chat-interface` helpers: cache read-through, persist, server sync."""

from __future__ import annotations

from datetime import datetime, timezone

from chat_interface.cache import MonthStore, month_key
from chat_interface.cli import (
    _apply_read_overlay,
    _load_read_overlay,
    _persist_read,
    _read_cache_direct,
    _save_read_overlay,
    _server_mark_read,
)
from chat_interface.synthetic import synthetic_messages
from chat_interface.threads import group_threads

UTC = timezone.utc


def _seed_cache(root, account="acct"):
    now = datetime.now(UTC)
    msgs = synthetic_messages((account,), seed=2, threads_per_account=6)
    for m in msgs:
        m.timestamp = now
        m.is_unread = True
    MonthStore(root, account).save(month_key(now), msgs)
    return msgs


def test_read_cache_direct_sees_disk_only_accounts(tmp_path):
    _seed_cache(tmp_path, "onlyondisk")
    msgs = _read_cache_direct(tmp_path, days=30)
    assert msgs and {m.account for m in msgs} == {"onlyondisk"}
    # account filter
    assert _read_cache_direct(tmp_path, days=30, accounts=["nope"]) == []


def test_persist_read_writes_through_to_parquet(tmp_path):
    msgs = _seed_cache(tmp_path)
    key = month_key(datetime.now(UTC))
    threads = group_threads(msgs)[:2]
    for t in threads:                       # browser flipped these in memory
        for m in t.messages:
            m.is_unread = False

    _persist_read(tmp_path, threads)

    reloaded = MonthStore(tmp_path, "acct").load(key)
    read_ids = {m.id for t in threads for m in t.messages}
    assert all(not m.is_unread for m in reloaded if m.id in read_ids)
    assert all(m.is_unread for m in reloaded if m.id not in read_ids)


def test_server_mark_read_dedupes_per_channel_and_collects_errors():
    threads = group_threads(synthetic_messages(("a",), seed=1, threads_per_account=10))
    calls: list[tuple] = []

    class FakeMgr:
        def mark_read(self, account, channel_id, *, up_to=None):
            calls.append((account, channel_id, up_to))
            if channel_id.endswith("0"):
                raise RuntimeError("boom")

    errs = _server_mark_read(FakeMgr(), threads)
    # one call per distinct (account, channel), not per thread
    assert len(calls) == len({(t.account, t.root.channel_id) for t in threads})
    # up_to is the latest activity among that channel's marked threads
    for account, channel_id, up_to in calls:
        peers = [t for t in threads if t.root.channel_id == channel_id]
        assert up_to == max(t.last_activity for t in peers)
    assert all(e[1] == "boom" for e in errs)


def test_read_overlay_roundtrip_and_apply(tmp_path):
    # empty / missing / corrupt -> empty set, no crash
    assert _load_read_overlay(tmp_path) == set()
    (tmp_path / "_read_overlay.json").write_text("not json")
    assert _load_read_overlay(tmp_path) == set()

    _save_read_overlay(tmp_path, {"a:001:0", "b:003:0"})
    assert _load_read_overlay(tmp_path) == {"a:001:0", "b:003:0"}
    _save_read_overlay(tmp_path, _load_read_overlay(tmp_path) | {"c:000:0"})
    assert _load_read_overlay(tmp_path) == {"a:001:0", "b:003:0", "c:000:0"}

    # _apply_read_overlay clears is_unread on every message of the listed threads,
    # by (thread_id or id) -- so it works for real replies too, not just roots
    msgs = synthetic_messages(("a", "b"), seed=1, threads_per_account=6)
    for m in msgs:
        m.is_unread = True
    _apply_read_overlay(msgs, _load_read_overlay(tmp_path))
    threads = {t.id: t for t in group_threads(msgs)}
    assert not threads["a:001:0"].is_unread
    assert not threads["b:003:0"].is_unread
    assert threads["a:002:0"].is_unread            # not in the overlay -> untouched


def test_manager_mark_read_dispatches_to_named_client():
    from chat_interface.manager import ChatManager

    seen = []

    class FakeClient:
        name = "work"

        def mark_read(self, channel_id, *, up_to=None):
            seen.append((channel_id, up_to))

    mgr = ChatManager.__new__(ChatManager)
    mgr.clients = [FakeClient()]
    mgr.mark_read("work", "C1", up_to=None)
    assert seen == [("C1", None)]


def test_manager_fetch_runs_accounts_in_parallel_and_isolates_errors():
    import threading
    import time

    from chat_interface.base import ChatClientError
    from chat_interface.filters import FetchFilter
    from chat_interface.manager import ChatManager
    from chat_interface.models import ChannelKind, Message

    live = 0
    peak = 0
    lock = threading.Lock()

    def _msg(acct):
        return Message(id=f"{acct}-1", text="x", timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                       author_id="u", author_name="u", channel_id="c", channel_name="c",
                       channel_kind=ChannelKind.PUBLIC, account=acct)

    class FakeClient:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        def fetch(self, flt):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.3)
            with lock:
                live -= 1
            if self.fail:
                raise ChatClientError("boom")
            return [_msg(self.name)]

    mgr = ChatManager.__new__(ChatManager)
    mgr.clients = [FakeClient("a"), FakeClient("b"), FakeClient("c", fail=True)]
    mgr.errors = {}

    t0 = time.time()
    msgs = mgr.fetch(FetchFilter(), cache=False)
    elapsed = time.time() - t0

    assert peak == 3                          # all three ran at once
    assert elapsed < 0.8                      # concurrent, not 3 x 0.3s serial
    assert {m.account for m in msgs} == {"a", "b"}   # "c" failed, others fine
    assert list(mgr.errors) == ["c"] and mgr.errors["c"] == "boom"
