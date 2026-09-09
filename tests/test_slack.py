import time
from datetime import datetime, timezone

import httpx
import pytest
import respx

from unichat.base import ChatClientError
from unichat.config import AccountConfig
from unichat.providers.slack import SlackClient

API = "https://slack.com/api"


def _account(**settings):
    return AccountConfig(name="ws", type="slack", settings={"token": "xoxp-test", **settings})


def _mock_auth(router):
    auth = router.get(f"{API}/auth.test").mock(
        return_value=httpx.Response(200, json={"ok": True, "user_id": "U1", "team_id": "T1", "url": "https://ws.slack.com/"})
    )
    router.get(f"{API}/users.list").mock(
        return_value=httpx.Response(200, json={"ok": True, "members": []})
    )
    router.get(f"{API}/conversations.info").mock(
        return_value=httpx.Response(200, json={"ok": True, "channel": {"last_read": "0"}})
    )
    router.get(f"{API}/client.userBoot").mock(
        return_value=httpx.Response(200, json={"ok": True, "channels": []})
    )
    router.get(f"{API}/users.info").mock(
        return_value=httpx.Response(200, json={"ok": True, "user": {"name": "someone"}})
    )
    return auth


def test_xoxc_token_requires_cookie():
    with pytest.raises(ChatClientError, match="cookie"):
        SlackClient(_account(token="xoxc-abc"))


def test_bad_token_prefix_rejected():
    with pytest.raises(ChatClientError, match="xoxp"):
        SlackClient(_account(token="not-a-slack-token"))


@respx.mock
def test_browser_session_sends_d_cookie():
    router = respx.mock
    auth = _mock_auth(router)
    # "d=..." prefix is tolerated and stripped to the bare value
    c = SlackClient(_account(token="xoxc-abc", cookie="d=xoxd-secret"))
    assert c.cookie == "xoxd-secret"
    c.ensure_auth()
    assert auth.calls.last.request.headers["cookie"] == "d=xoxd-secret"
    assert auth.calls.last.request.headers["authorization"] == "Bearer xoxc-abc"
    c.close()


@respx.mock
def test_mark_read_uses_conversations_mark_with_ts():
    router = respx.mock
    _mock_auth(router)
    mark = router.post(f"{API}/conversations.mark").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    c = SlackClient(_account())
    up_to = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    c.mark_read("C123", up_to=up_to)

    assert mark.called
    body = dict(pair.split("=") for pair in mark.calls.last.request.content.decode().split("&"))
    assert body["channel"] == "C123"
    assert body["ts"] == f"{up_to.timestamp():.6f}"
    assert c._last_read["C123"] == up_to.timestamp()
    c.close()


@respx.mock
def test_mark_read_without_up_to_reads_latest_ts():
    router = respx.mock
    _mock_auth(router)
    router.get(f"{API}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": [{"ts": "1725000000.001200"}]})
    )
    mark = router.post(f"{API}/conversations.mark").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    c = SlackClient(_account())
    c.mark_read("C9")
    body = dict(pair.split("=") for pair in mark.calls.last.request.content.decode().split("&"))
    assert body["ts"] == "1725000000.001200"
    c.close()


# --- speedups: client.counts activity probe (#1) + reply gating (#2) ----------

def _channel(cid="C1"):
    from unichat.models import Channel, ChannelKind
    return Channel(id=cid, name=cid, kind=ChannelKind.PUBLIC, account="ws", provider="slack")


WIN_START = datetime(2026, 9, 1, tzinfo=timezone.utc)
WIN_END = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _mock_counts(router, **latest_by_id):
    return router.get(f"{API}/client.counts").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "channels": [{"id": cid, "latest": ts, "last_read": "1.0"}
                         for cid, ts in latest_by_id.items()],
            "mpims": [], "ims": [],
        })
    )


@respx.mock
def test_activity_probe_skips_dormant_channels():
    router = respx.mock
    _mock_auth(router)
    before = f"{(WIN_START.timestamp() - 86400):.6f}"      # a day before the window
    counts = _mock_counts(router, C_dead=before)
    hist = router.get(f"{API}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": []})
    )
    c = SlackClient(_account())
    assert list(c._iter_channel_between(_channel("C_dead"), WIN_START, WIN_END)) == []
    assert counts.called
    assert not hist.called                                 # channel skipped entirely
    assert c._last_read["C_dead"] == 1.0                    # last_read still harvested
    c.close()


@respx.mock
def test_activity_probe_scans_active_and_unknown_channels():
    router = respx.mock
    _mock_auth(router)
    inside = f"{(WIN_START.timestamp() + 3600):.6f}"
    _mock_counts(router, C_live=inside)                     # C_unknown absent from counts
    hist = router.get(f"{API}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": []})
    )
    c = SlackClient(_account())
    list(c._iter_channel_between(_channel("C_live"), WIN_START, WIN_END))
    list(c._iter_channel_between(_channel("C_unknown"), WIN_START, WIN_END))
    assert {call.request.url.params["channel"] for call in hist.calls} == {"C_live", "C_unknown"}
    c.close()


@respx.mock
def test_probe_failure_falls_back_to_scanning_everything():
    router = respx.mock
    _mock_auth(router)
    for m in ("client.counts", "client.userBoot"):
        router.get(f"{API}/{m}").mock(
            return_value=httpx.Response(200, json={"ok": False, "error": "method_not_supported"})
        )
    hist = router.get(f"{API}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": []})
    )
    c = SlackClient(_account())
    list(c._iter_channel_between(_channel("C1"), WIN_START, WIN_END))
    assert c._activity() is None                            # both probes unavailable
    assert hist.called
    c.close()


@respx.mock
def test_userboot_updated_extends_probe_coverage():
    router = respx.mock
    _mock_auth(router)
    _mock_counts(router)                                    # counts knows nothing
    old_ms = int((WIN_START.timestamp() - 3600) * 1000)     # 'updated' is milliseconds
    router.get(f"{API}/client.userBoot").mock(
        return_value=httpx.Response(200, json={"ok": True, "channels": [
            {"id": "C_boot_old", "updated": old_ms},
        ]})
    )
    hist = router.get(f"{API}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": []})
    )
    c = SlackClient(_account())
    assert list(c._iter_channel_between(_channel("C_boot_old"), WIN_START, WIN_END)) == []
    assert not hist.called                                  # skipped via userBoot.updated
    c.close()


@respx.mock
def test_replies_fetched_only_for_threads_active_in_window():
    router = respx.mock
    _mock_auth(router)
    _mock_counts(router, C1=f"{(WIN_END.timestamp()):.6f}")
    stale = f"{(WIN_START.timestamp() - 10):.6f}"
    fresh = f"{(WIN_START.timestamp() + 10):.6f}"
    router.get(f"{API}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": [
            {"type": "message", "ts": "100.0", "text": "old thread", "reply_count": 3, "latest_reply": stale},
            {"type": "message", "ts": "200.0", "text": "live thread", "reply_count": 1, "latest_reply": fresh},
        ]})
    )
    replies = router.get(f"{API}/conversations.replies").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": [
            {"type": "message", "ts": fresh, "text": "a reply"},
        ]})
    )
    c = SlackClient(_account())
    list(c._iter_channel_between(_channel("C1"), WIN_START, WIN_END))
    assert replies.call_count == 1
    assert replies.calls.last.request.url.params["ts"] == "200.0"
    c.close()


# --- parallel messages_between (#3) ------------------------------------------

@respx.mock
def test_messages_between_runs_channels_in_parallel_and_survives_one_failure():
    router = respx.mock
    _mock_auth(router)
    _mock_counts(router)                                    # nothing skipped
    router.get(f"{API}/client.userBoot").mock(
        return_value=httpx.Response(200, json={"ok": True, "channels": []})
    )

    def history(request):
        cid = request.url.params["channel"]
        if cid == "C_bad":
            return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
        return httpx.Response(200, json={"ok": True, "messages": [
            {"type": "message", "ts": "1756800000.0", "text": f"hi from {cid}", "user": "U9"},
        ]})

    router.get(f"{API}/conversations.history").mock(side_effect=history)

    c = SlackClient(_account(concurrency=4))
    assert c._concurrency == 4
    chans = [_channel(f"C{i}") for i in range(6)] + [_channel("C_bad")]
    seen = []
    msgs = list(c.messages_between(WIN_START, WIN_END, chans,
                                   on_channel=lambda ch, i, n: seen.append((ch.id, i, n))))

    got = {m.text for m in msgs}
    assert got == {f"hi from C{i}" for i in range(6)}       # C_bad skipped, rest kept
    assert len(seen) == 7 and {n for _, _, n in seen} == {7}
    c.close()


def test_rate_limiter_blocks_past_the_window():
    from unichat.providers.slack import _RateLimiter
    rl = _RateLimiter(per_minute=1000)                      # generous -> never sleeps
    t0 = time.monotonic()
    for _ in range(50):
        rl.acquire()
    assert time.monotonic() - t0 < 0.5

    rl2 = _RateLimiter(per_minute=2)
    rl2.acquire()
    rl2.acquire()                                           # bucket now full
    assert len(rl2._hits) == 2
