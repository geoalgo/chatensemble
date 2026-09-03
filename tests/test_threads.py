"""Thread grouping, synthetic data, and the browser's state machine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rich.console import Console

from chat_interface.browser import ThreadBrowser, _reaction_line, _truncate
from chat_interface.models import ChannelKind, Message
from chat_interface.synthetic import synthetic_messages
from chat_interface.threads import group_threads, is_system_message

UTC = timezone.utc


def _msg(mid, ts, *, thread=None, account="a", chan="c", unread=False):
    return Message(
        id=mid, text=f"msg {mid}", timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=ts),
        author_id=f"u{mid}", author_name=f"User {mid}", channel_id=chan, channel_name=chan,
        channel_kind=ChannelKind.PUBLIC, account=account, thread_id=thread, is_unread=unread,
    )


def test_group_threads_roots_and_replies():
    msgs = [
        _msg("root", 0, thread="root"),
        _msg("r1", 5, thread="root"),
        _msg("r2", 3, thread="root"),
        _msg("lonely", 10),
    ]
    threads = group_threads(msgs, newest_first=False)
    assert len(threads) == 2
    conv = next(t for t in threads if t.id == "root")
    assert conv.root.id == "root"
    assert [m.id for m in conv.replies] == ["r2", "r1"]  # time-ordered
    assert conv.reply_count == 2
    assert conv.last_activity == msgs[1].timestamp
    lonely = next(t for t in threads if t.id == "lonely")
    assert lonely.reply_count == 0


def test_group_threads_orphan_reply_gets_earliest_root():
    msgs = [_msg("b", 8, thread="missing"), _msg("a", 2, thread="missing")]
    (thread,) = group_threads(msgs)
    assert thread.root.id == "a"
    assert [m.id for m in thread.replies] == ["b"]


def test_group_threads_unread_and_participants():
    msgs = [_msg("root", 0, thread="root"), _msg("r1", 1, thread="root", unread=True)]
    (t,) = group_threads(msgs)
    assert t.is_unread and t.unread_count == 1
    assert t.participants == ["User root", "User r1"]


def test_group_threads_drops_system_messages():
    msgs = [
        _msg("root", 0, thread="root"),
        _msg("r1", 2, thread="root"),
        Message(
            id="sys", text="joaquin_vanschoren joined the channel.",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC), author_id="u", author_name="joaquin",
            channel_id="c", channel_name="c", channel_kind=ChannelKind.PUBLIC, account="a",
        ),
    ]
    assert is_system_message(msgs[2]) is True
    threads = group_threads(msgs)
    assert len(threads) == 1
    assert all("joined the channel" not in m.text for t in threads for m in t.messages)
    assert group_threads(msgs, drop_system=False)[0].id in {"root", "sys"}
    assert len(group_threads(msgs, drop_system=False)) == 2


def test_truncate_appends_literal_ellipsis():
    assert _truncate("short", 20) == "short"
    out = _truncate("x" * 50, 20)
    assert out.endswith("...") and len(out) == 20


def test_reaction_line_renders_emoji():
    from chat_interface.display import _emojize

    thumb, rocket, tada, heart = (_emojize(f":{n}:") for n in ("+1", "rocket", "tada", "heart"))
    assert thumb == "\N{THUMBS UP SIGN}" and ":" not in tada  # shortcodes actually resolved

    assert _reaction_line({"+1": 1}) == thumb                    # lone, count 1 -> bare glyph
    assert _reaction_line({"rocket": 3}) == f"{rocket} (3)"      # lone, count>1 -> parens
    assert _reaction_line({"tada": 4, "heart": 2}) == f"{tada} (4)  {heart} (2)"
    assert _reaction_line({"totally_made_up": 1, "eyes": 1}).startswith(":totally_made_up: (1)")


def test_synthetic_is_deterministic_and_multi_account():
    a = synthetic_messages(("x", "y"), seed=7, threads_per_account=5)
    b = synthetic_messages(("x", "y"), seed=7, threads_per_account=5)
    assert [m.id for m in a] == [m.id for m in b]
    assert {m.account for m in a} == {"x", "y"}

    threads = group_threads(a)
    assert len(threads) == 10  # 5 per account
    assert all(t.reply_count == len(t.replies) for t in threads)
    # replies never precede their root
    for t in threads:
        assert all(r.timestamp >= t.root.timestamp for r in t.replies)


def test_synthetic_default_accounts_are_distinct():
    from chat_interface.synthetic import DEFAULT_ACCOUNTS

    msgs = synthetic_messages(seed=3, threads_per_account=10)
    assert {m.account for m in msgs} == set(DEFAULT_ACCOUNTS)

    chans = {a: {m.channel_name for m in msgs if m.account == a} for a in DEFAULT_ACCOUNTS}
    people = {a: {m.author_name for m in msgs if m.account == a} for a in DEFAULT_ACCOUNTS}
    accts = list(DEFAULT_ACCOUNTS)
    for i in range(len(accts)):
        for j in range(i + 1, len(accts)):
            assert not (chans[accts[i]] & chans[accts[j]]), "channels overlap"
            assert not (people[accts[i]] & people[accts[j]]), "people overlap"


def test_browser_navigation_state_machine():
    msgs = synthetic_messages(("x", "y", "z"), seed=1, threads_per_account=6)
    b = ThreadBrowser(msgs, console=Console(width=100, height=40))
    assert b.tab_names[:2] == ["Unread", "All"]
    assert len(b.tab_names) == 5                 # Unread, All, x, y, z

    b.handle("3")                                # jump to 3rd tab (account "x")
    assert b.tab_idx == 2 and b.tab_names[2] == "x" and b.sel == 0
    b.handle("DOWN")
    b.handle("DOWN")
    b._clamp_list()
    assert b.sel == 2

    b.handle("ENTER")                            # open thread
    assert b.view == "thread"
    b.handle("G")                                # scroll to end, clamped on render
    b.render()
    assert b.scroll >= 0
    b.handle("]")                                # next thread, still in thread view
    assert b.view == "thread" and b.sel == 3
    assert b.handle("q") is True and b.view == "list"   # q backs out of thread
    assert b.handle("q") is False                        # q quits from list


def test_browser_unread_tab_is_first_and_holds_every_unread_thread():
    msgs = synthetic_messages(("x", "y", "z"), seed=1, threads_per_account=8)
    b = ThreadBrowser(msgs, console=Console(width=100, height=40))

    assert b.tab_names[0] == "Unread"
    unread_tab = b.tab_threads[0]
    all_tab = b.tab_threads[1]
    assert unread_tab                             # this seed has unread threads
    assert b.tab_idx == 0                         # starts there when non-empty
    assert all(t.is_unread for t in unread_tab)
    assert unread_tab == [t for t in all_tab if t.is_unread]

    # empty-unread data starts on "All" instead
    for m in msgs:
        m.is_unread = m.is_mention = False
    b2 = ThreadBrowser(msgs, console=Console(width=100, height=40))
    assert b2.tab_threads[0] == [] and b2.tab_idx == 1


def test_browser_mark_read():
    msgs = synthetic_messages(("x", "y"), seed=1, threads_per_account=8)
    seen: list = []
    b = ThreadBrowser(msgs, console=Console(width=100, height=40), on_read=seen.append)
    assert b.tab_idx == 0                         # Unread
    n0 = len(b.tab_threads[0])
    assert n0 >= 2

    target = b.current
    assert target.is_unread
    b.handle("x")                                 # mark the selected thread read
    assert not target.is_unread
    assert all(not m.is_unread for m in target.messages)
    assert target not in b.tab_threads[0]         # gone from Unread
    assert target in b.tab_threads[1]             # still in All, just read
    assert len(b.tab_threads[0]) == n0 - 1
    assert seen == [target]

    b.handle("X")                                 # mark everything left in the tab read
    assert b.tab_threads[0] == []
    assert all(not t.is_unread for t in b.tab_threads[1])
    assert len(seen) == n0                        # on_read fired once per thread, no repeats

    b.handle("x")                                 # nothing unread -> no-op, no callback
    assert len(seen) == n0


def test_browser_open_in_web(monkeypatch):
    import chat_interface.browser as br

    opened: list = []
    monkeypatch.setattr(br.webbrowser, "open", lambda url, new=0: opened.append(url) or True)

    msgs = synthetic_messages(("x",), seed=1, threads_per_account=4)
    b = ThreadBrowser(msgs, console=Console(width=100, height=40))

    b.handle("o")                                 # "o" opens the permalink, NOT the thread view
    assert b.view == "list"
    assert opened == [b.current.root.permalink]
    assert "browser" in b._status

    b.handle("ENTER")                             # thread view is still on Enter
    assert b.view == "thread"
    b.handle("o")
    assert opened[-1] == b.current.root.permalink

    # no permalink -> a note, no call
    opened.clear()
    b.current.root.permalink = None
    b.handle("o")
    assert opened == [] and "no web link" in b._status


def _console():
    import io

    return Console(file=io.StringIO(), width=100, height=40, force_terminal=True)


def test_browser_refetch_reloads_and_keeps_position():
    v1 = synthetic_messages(("x", "y"), seed=1, threads_per_account=6)
    v2 = synthetic_messages(("x", "y"), seed=1, threads_per_account=6)  # same ids
    for m in v2:                                   # a genuinely fresh message set
        m.is_unread = False

    calls = []

    def refetch():
        calls.append(1)
        return list(v2)

    b = ThreadBrowser(v1, console=_console(), on_refetch=refetch)
    b.handle("2")                                  # -> "All" tab
    b.handle("DOWN")
    b.handle("DOWN")
    b._clamp_list()
    keep_tab = b.tab_names[b.tab_idx]
    keep_id = b.current.id

    b.handle("u")
    assert calls == [1]
    assert b.tab_names[b.tab_idx] == keep_tab      # same tab, by name
    assert b.current.id == keep_id                 # same thread, by id
    assert b.tab_threads[0] == []                  # v2 has nothing unread
    assert "refresh" in b._status

    b.handle("j")                                  # any key clears the note
    assert b._status == ""


def test_browser_refetch_failure_keeps_current_view():
    msgs = synthetic_messages(("x",), seed=1, threads_per_account=5)

    def boom():
        raise RuntimeError("network down")

    b = ThreadBrowser(msgs, console=_console(), on_refetch=boom)
    before = list(b.tab_threads[1])
    b.handle("u")
    assert b.tab_threads[1] == before              # unchanged
    assert "network down" in b._status

    b2 = ThreadBrowser(msgs, console=_console(), on_refetch=lambda: [])
    b2.handle("u")
    assert b2.tab_threads[1] == before
    assert "nothing" in b2._status

    # no on_refetch -> "u" is a harmless no-op
    b3 = ThreadBrowser(msgs, console=_console())
    b3.handle("u")
    assert b3._status == "no live account to fetch"


def test_browser_starts_at_top_and_fits_screen():
    msgs = synthetic_messages(("x", "y", "z"), seed=4, threads_per_account=12)
    con = Console(width=120, height=30)
    b = ThreadBrowser(msgs, console=con)
    assert (b.sel, b.offset) == (0, 0)          # first row selected, not scrolled

    lines = con.render_lines(b.render(), con.options.update(height=None), pad=False)
    assert len(lines) <= con.size.height        # never taller than the screen
    assert "Unread" in "".join(seg.text for seg in lines[0])  # tab bar on row 0

    # paging down then Home returns to the very top
    b.handle("PGDN")
    b._clamp_list()
    b.handle("HOME")
    b._clamp_list()
    assert (b.sel, b.offset) == (0, 0)


def test_browser_keyboard_tab_switch():
    b = ThreadBrowser(synthetic_messages(("x", "y"), seed=2, threads_per_account=4),
                      console=Console(width=120, height=40))
    b.handle("3")                              # 3rd tab -> index 2
    assert b.tab_idx == 2 and b.tab_names[2] == "x"
    b.handle("LEFT")
    assert b.tab_idx == 1 and b.tab_names[1] == "All"
