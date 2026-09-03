"""Interactive terminal thread browser (rich).

Skim threads across several accounts: a tab bar picks the account (``All``
merges every account), the thread list scrolls, and opening a thread shows the
full conversation with its own scroll.

Keyboard only (so terminal text selection / copy-paste keeps working):
  left/right or 1-9  switch account · up/down or j/k  move · enter  open thread
  f  find messages by author/text · o  open the permalink in a web browser
  x / X  mark read · u  refetch · q  quit

Falls back to a plain per-account feed when stdin/stdout is not a real terminal.
"""

from __future__ import annotations

import os
import re
import select
import sys
import unicodedata
import webbrowser
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone

from rich.console import Console, Group
from rich.segment import Segment, Segments
from rich.table import Table
from rich.text import Text

from .display import _clean, _emojize, _relative_time, print_feed
from .models import Message
from .threads import Thread, group_threads

_CARD_H = 3          # fixed lines per thread card
_SNIPPET_MAX = 200   # hard cap on the preview length, before the terminal width
_FIND_MAX = 1000     # cap on "f" search results

# Everything drawn as "chrome" is ASCII-only. Non-ASCII markers (★ ● ▸ 👥 ─)
# are East-Asian *ambiguous / wide*: many terminals render them 2 cells wide
# while rich counts them as 1, so every line drifts, overflows and wraps.
#
# The selected row is shown by reverse video and unread by a bold author name --
# no per-row prefix character. Only an @mention gets a leading "! ".
_MARK_MENTION = "! "
_MARK_NONE = "  "


def _ascii_safe(s: str) -> str:
    """Drop control/zero-width chars and squash wide glyphs to one cell.

    Accented Latin (Ondrej, Vojtechova, Jorg) is narrow and kept as-is; only
    genuinely wide code points (emoji, CJK, full-width) are replaced, so a
    line's rendered width always equals len() and never drifts past the edge.
    """
    if s.isascii():
        return s
    out: list[str] = []
    for ch in s:
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            continue
        out.append("*" if unicodedata.east_asian_width(ch) in ("W", "F") else ch)
    return "".join(out)


def _truncate(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: max(1, n - 3)].rstrip() + "..."


def _highlight_terms(text: Text, query: str, style: str = "black on yellow") -> None:
    for term in query.split():
        if term:
            text.highlight_words([term], style, case_sensitive=False)


def _reaction_line(reactions: dict[str, int]) -> str:
    """``{"tada": 4, "heart": 1}`` -> ``"🎉 (4)  ❤️ (1)"``.

    A lone reaction with a count of 1 is shown as the bare glyph; otherwise each
    gets its count in parentheses. Unknown shortcodes fall back to ``:name:``.
    """
    items = list(reactions.items())[:12]
    show_counts = len(items) > 1 or any(v > 1 for _, v in items)
    out = []
    for name, count in items:
        glyph = _emojize(f":{name}:")
        out.append(f"{glyph} ({count})" if show_counts else glyph)
    return "  ".join(out)


# --------------------------------------------------------------------------- #
# raw terminal input  (keyboard only -- mouse reporting is left OFF so the
# terminal's own text selection / copy-paste keeps working)
# --------------------------------------------------------------------------- #
_CSI_LETTER = re.compile(rb"\x1b[\[O]([A-Za-z])")
_CSI_TILDE = re.compile(rb"\x1b\[([0-9;]+)~")
_ARROWS = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "H": "HOME", "F": "END"}
_TILDES = {"1": "HOME", "4": "END", "5": "PGUP", "6": "PGDN", "3": "DEL"}


class _Input:
    """Parse a raw tty byte stream into keyboard event tokens."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.buf = b""

    def _fill(self, timeout: float) -> bool:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return False
        chunk = os.read(self.fd, 4096)
        if not chunk:
            return False
        self.buf += chunk
        return True

    def flush(self) -> None:
        """Drop anything already typed (shell type-ahead, the launch Enter)."""
        while self._fill(0.05):
            pass
        self.buf = b""

    def next(self, timeout: float = 0.2):
        """Block up to ``timeout`` for one event; return a token or ``None``.

        Tokens: ``"UP" "DOWN" "LEFT" "RIGHT" "ENTER" "ESC" "BACKSPACE" "HOME"
        "END" "PGUP" "PGDN" "TAB" "CTRL-C"`` or a single character.
        """
        if not self.buf and not self._fill(timeout):
            return None
        if self.buf[:1] != b"\x1b":
            ch, self.buf = self.buf[:1], self.buf[1:]
            return self._simple(ch)

        # escape sequence -- give a partial one a moment to arrive
        if len(self.buf) < 3:
            self._fill(0.02)
        b = self.buf
        if b == b"\x1b":
            self.buf = b""
            return "ESC"
        if b[:3] == b"\x1b[<":
            # a stray mouse report (reporting is off, but be defensive): swallow
            end = b.find(b"M", 3)
            end = end if end != -1 else b.find(b"m", 3)
            if end == -1:
                if self._fill(0.03):
                    return self.next(0)
                self.buf = b""
                return None
            self.buf = b[end + 1:]
            return None
        m = _CSI_TILDE.match(b)
        if m:
            self.buf = b[m.end():]
            return _TILDES.get(m[1].split(b";")[0].decode())
        m = _CSI_LETTER.match(b)
        if m:
            self.buf = b[m.end():]
            return _ARROWS.get(m[1].decode())
        # unrecognised CSI (bracketed paste markers, etc.) -- drop the ESC
        self.buf = b[1:]
        return "ESC"

    @staticmethod
    def _simple(ch: bytes):
        return {
            b"\r": "ENTER", b"\n": "ENTER", b"\x7f": "BACKSPACE", b"\x08": "BACKSPACE",
            b"\x03": "CTRL-C", b"\t": "TAB",
        }.get(ch) or (ch.decode(errors="ignore") or None)


_MOUSE_OFF = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l"


@contextmanager
def _raw_terminal(console: Console):
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    out = console.file
    try:
        tty.setraw(fd)
        out.write(_MOUSE_OFF)          # drop any tracking left on by a crashed run
        out.write("\x1b[?1049h")       # alternate screen (own it ourselves, not via rich)
        out.write("\x1b[2J\x1b[H")     # clear + home so frame 1 starts at the top row
        out.write("\x1b[?25l")         # hide cursor
        out.flush()
        yield _Input(fd)
    finally:
        out.write(_MOUSE_OFF)
        out.write("\x1b[?25h")         # show cursor
        out.write("\x1b[?1049l")       # back to the normal screen
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


# --------------------------------------------------------------------------- #
# browser
# --------------------------------------------------------------------------- #
class ThreadBrowser:
    def __init__(
        self,
        messages: list[Message],
        *,
        console: Console | None = None,
        on_read: Callable[[Thread], None] | None = None,
        on_refetch: Callable[[], list[Message]] | None = None,
    ) -> None:
        self.console = console or Console()
        # called once per thread when the user marks it read; a caller can use it
        # to push the read marker to the server and/or the on-disk cache.
        self._on_read = on_read
        # called when the user hits "u": returns a fresh message list to reload.
        self._on_refetch = on_refetch

        self.tab_idx = 0
        self.sel = 0
        self.offset = 0
        self.view = "list"           # "list" | "thread" | "find"
        self.scroll = 0
        self._status = ""            # transient one-line note in the help bar
        self._scroll_to: str | None = None            # msg id to scroll the thread to
        self._thread_mark: tuple[str, str] | None = None  # (thread id, msg id) to flag
        # find ("f"): query editing + a flat list of matching messages
        self._find_q = ""
        self._find_editing = False
        self._find_results: list[Message] = []
        self._find_sel = 0
        self._find_offset = 0
        self._load(messages)
        self._clamp_list()

    def _load(self, messages: list[Message], *, keep_position: bool = False) -> None:
        """(Re)build the tab structure from ``messages``.

        With ``keep_position`` the current tab (by name) and selected thread
        (by id) are re-resolved against the new data.
        """
        prev_tab = self.tab_names[self.tab_idx] if keep_position else None
        prev_id = self.current.id if keep_position and self.current else None

        all_threads = group_threads(messages)
        accounts = sorted({t.account for t in all_threads})
        unread = [t for t in all_threads if t.is_unread]
        self.tab_names: list[str] = ["Unread", "All", *accounts]
        self.tab_threads: list[list[Thread]] = [unread, all_threads] + [
            [t for t in all_threads if t.account == a] for a in accounts
        ]

        if keep_position and prev_tab in self.tab_names:
            self.tab_idx = self.tab_names.index(prev_tab)
        else:
            self.tab_idx = 0 if unread else 1     # Unread when non-empty, else All
        if prev_id is not None:
            hit = next((i for i, t in enumerate(self.threads) if t.id == prev_id), None)
            self.sel = hit if hit is not None else 0
        else:
            self.sel = 0
        self.offset = 0

    # -- current selection helpers ------------------------------------- #
    @property
    def threads(self) -> list[Thread]:
        return self.tab_threads[self.tab_idx]

    @property
    def _cross_account(self) -> bool:
        """The current tab mixes accounts, so cards should name the account."""
        return self.tab_names[self.tab_idx] in ("Unread", "All")

    @property
    def current(self) -> Thread | None:
        return self.threads[self.sel] if self.threads else None

    def _visible_cards(self) -> int:
        # tab bar (1) + rule (1) + help line (1) frame the card area
        return max(1, (self.console.size.height - 3) // _CARD_H)

    def _clamp_list(self) -> None:
        n = len(self.threads)
        self.sel = 0 if n == 0 else max(0, min(self.sel, n - 1))
        vis = self._visible_cards()
        if self.sel < self.offset:
            self.offset = self.sel
        elif self.sel >= self.offset + vis:
            self.offset = self.sel - vis + 1
        self.offset = max(0, min(self.offset, max(0, n - vis)))

    def _select_tab(self, idx: int) -> None:
        self.tab_idx = max(0, min(idx, len(self.tab_names) - 1))
        self.sel = self.offset = 0

    def _clamp_find(self) -> None:
        n = len(self._find_results)
        self._find_sel = 0 if n == 0 else max(0, min(self._find_sel, n - 1))
        vis = self._visible_cards()
        if self._find_sel < self._find_offset:
            self._find_offset = self._find_sel
        elif self._find_sel >= self._find_offset + vis:
            self._find_offset = self._find_sel - vis + 1
        self._find_offset = max(0, min(self._find_offset, max(0, n - vis)))

    # -- mark read --------------------------------------------------- #
    def _mark_read(self, threads: "Thread | list[Thread] | None") -> None:
        """Clear the unread flag on one or more threads (client-side, optimistic).

        The change is immediate: the thread leaves the ``Unread`` tab and its
        author stops rendering bold. ``on_read`` is invoked once per thread so a
        caller can persist it (server read marker, cache rewrite).
        """
        if threads is None:
            return
        if isinstance(threads, Thread):
            threads = [threads]
        for t in threads:
            if not t.is_unread:
                continue
            for m in t.messages:
                m.is_unread = False
            if self._on_read is not None:
                self._on_read(t)
        # the Unread tab is derived state -- rebuild it
        self.tab_threads[0] = [t for t in self.tab_threads[0] if t.is_unread]
        self._clamp_list()

    # -- refetch ("u") --------------------------------------------- #
    def _refetch(self) -> None:
        """Pull fresh data via ``on_refetch`` and reload in place."""
        if self._on_refetch is None:
            self._status = "no live account to fetch"
            return
        self._status = "fetching..."
        self._paint()                       # show the note before the blocking call
        try:
            messages = self._on_refetch()
        except Exception as exc:            # network / auth / provider error
            self._status = f"fetch failed: {exc}"[:70]
            return
        if not messages:
            self._status = "fetch returned nothing -- kept current view"
            return
        self._load(messages, keep_position=True)
        if self.view == "thread" and self.current is None:
            self.view = "list"
        if self.view == "find":                 # stale message refs -> drop back
            self.view = "list"
            self._find_results = []
        self._clamp_list()
        self._status = "refreshed"

    # -- find ("f") ----------------------------------------------------- #
    def _start_find(self) -> None:
        self.view = "find"
        self._find_editing = True
        self._run_find()

    def _run_find(self) -> None:
        """(Re)compute matching messages from the current query (live)."""
        terms = self._find_q.lower().split()
        hits: list[Message] = []
        if terms:
            for t in self.tab_threads[1]:       # the "All" tab == every loaded thread
                for m in t.messages:
                    hay = f"{m.author_name}\n{_clean(m.text)}".lower()
                    if all(term in hay for term in terms):
                        hits.append(m)
            hits.sort(key=lambda m: m.timestamp, reverse=True)
            del hits[_FIND_MAX:]
        self._find_results = hits
        self._find_sel = self._find_offset = 0

    def _handle_find_edit(self, ev) -> None:
        if ev == "ENTER":
            self._find_editing = False         # freeze; results are already live
        elif ev == "ESC":
            self.view = "list"
            self._find_editing = False
        elif ev == "BACKSPACE":
            self._find_q = self._find_q[:-1]
            self._run_find()
        elif isinstance(ev, str) and len(ev) == 1 and ev.isprintable():
            self._find_q += ev
            self._run_find()

    def _handle_find(self, ev) -> None:
        page = self._visible_cards()
        n = len(self._find_results)
        if ev in ("ESC", "q", "LEFT", "h", "BACKSPACE"):
            self.view = "list"
        elif ev in ("f", "/"):
            self._find_editing = True
        elif ev in ("DOWN", "j"):
            self._find_sel += 1
        elif ev in ("UP", "k"):
            self._find_sel -= 1
        elif ev in ("PGDN", " "):
            self._find_sel += page
        elif ev == "PGUP":
            self._find_sel -= page
        elif ev in ("HOME", "g"):
            self._find_sel = 0
        elif ev in ("END", "G"):
            self._find_sel = n - 1
        elif ev in ("ENTER", "RIGHT", "l"):
            self._open_find_result()
        elif ev == "o":
            self._open_web_msg(self._find_results[self._find_sel] if n else None)

    def _open_find_result(self) -> None:
        if not self._find_results:
            return
        m = self._find_results[self._find_sel]
        tid = m.thread_id or m.id
        # jump to the account's own tab first (thread is guaranteed present there)
        self.tab_idx = next(
            (i for i, name in enumerate(self.tab_names) if name == m.account), 1
        )
        hit = next((i for i, t in enumerate(self.threads)
                    if t.id == tid and t.account == m.account), None)
        if hit is None:                          # fall back to "All"
            self.tab_idx = 1
            hit = next((i for i, t in enumerate(self.threads) if t.id == tid), None)
        if hit is None:
            self.view = "list"
            self._status = "that message's thread is not loaded"
            return
        self.sel = hit
        self.scroll = 0
        self._scroll_to = m.id
        self._thread_mark = (tid, m.id)
        self.view = "thread"

    # -- rendering (ASCII-only chrome; see _ascii_safe) --------------- #
    def _width(self) -> int:
        # leave one spare column so a stray wide glyph can never wrap the line
        return max(20, self.console.size.width - 1)

    def _fit(self, text: Text) -> Text:
        text.no_wrap = True
        text.overflow = "ellipsis"
        return text

    def _tab_bar(self) -> Text:
        bar = Text(overflow="ellipsis", no_wrap=True)
        for i, name in enumerate(self.tab_names):
            threads = self.tab_threads[i]
            unread = sum(1 for t in threads if t.is_unread)
            seg = f" {_ascii_safe(name)} ({len(threads)})"
            if unread:
                seg += " *"
            seg += " "
            if i == self.tab_idx:
                style = "bold white on dodger_blue3"
            elif unread:
                style = "bold cyan"
            else:
                style = "grey62"
            bar.append(seg, style=style)
            bar.append(" ")
        return bar

    def _card(self, t: Thread, *, selected: bool, width: int) -> Table:
        g = Table.grid(padding=0)
        g.add_column(width=width, no_wrap=True, overflow="ellipsis")

        # line 1:  Author   [ channel ]   3 minutes ago   (2 replies)   account
        # selection = reverse video; unread = bold author; mention = leading "! "
        head = self._fit(Text())
        head.append(_MARK_MENTION if t.is_mention else _MARK_NONE,
                    style="bold red" if t.is_mention else "")
        head.append(_ascii_safe(t.root.author_name) or "(unknown)",
                    style="bold" if t.is_unread else "grey70")
        head.append("  ")
        head.append(f" {_ascii_safe(_clean(t.channel_name))} ", style="grey74 on grey23")
        head.append(f"  {_relative_time(t.last_activity)}", style="dim")
        if t.reply_count:
            n_new = t.unread_count - (1 if t.root.is_unread else 0)
            word = "reply" if t.reply_count == 1 else "replies"
            head.append(f"  ({t.reply_count} {word})", style="blue" if n_new > 0 else "dim")
        if self._cross_account:
            head.append(f"  {_ascii_safe(t.account)}", style="dim")

        # line 2:  message preview, first N chars, "..." when truncated
        limit = min(_SNIPPET_MAX, max(20, width - 2))
        preview = _truncate(_ascii_safe(_clean(t.root.text)) or "(no text)", limit)
        body = self._fit(Text(preview))
        body.highlight_regex(r"@[\w][\w.\-]*", "cyan")
        body.highlight_regex(r"@(?:channel|here|all|everyone)\b", "bold cyan")

        if selected:
            head.stylize("reverse")
            body.stylize("reverse")
        g.add_row(head)
        g.add_row(body)
        g.add_row(Text(""))  # spacer keeps card height == _CARD_H
        return g

    def _list_view(self) -> Group:
        width = self._width()
        parts: list = [self._tab_bar(), Text("-" * width, style="grey37")]
        if not self.threads:
            empty = ("  Nothing unread." if self.tab_names[self.tab_idx] == "Unread"
                     else "  No threads here.")
            parts.append(Text(empty, style="dim"))
        else:
            vis = self._visible_cards()
            window = self.threads[self.offset:self.offset + vis]
            for i, t in enumerate(window, start=self.offset):
                parts.append(self._card(t, selected=(i == self.sel), width=width))
        parts.append(self._help_line())
        return Group(*parts)

    def _help_line(self) -> Text:
        n = len(self.threads)
        pos = f"{self.sel + 1}/{n}" if n else "0/0"
        keys = ("[<-/->] account   [j k] move   [enter] open   "
                "[f] find   [o] web   [x] read   [q] quit")
        if self._on_refetch is not None:
            keys = keys.replace("[q] quit", "[u] fetch   [q] quit")
        tail = f"[ {self._status} ]" if self._status else keys
        return self._fit(Text(
            f" {_ascii_safe(self.tab_names[self.tab_idx])}  -  {pos}   {tail}",
            style="dim",
        ))

    def _result_card(self, m: Message, *, selected: bool, width: int) -> Table:
        """One search hit: author + channel + account + time, then a text preview."""
        g = Table.grid(padding=0)
        g.add_column(width=width, no_wrap=True, overflow="ellipsis")
        now = datetime.now(timezone.utc)

        head = self._fit(Text("  "))
        head.append(_ascii_safe(m.author_name) or "(unknown)", style="bold")
        head.append(f"  {_ascii_safe(_clean(m.channel_name))} ", style="grey74 on grey23")
        head.append(f"  {_relative_time(m.timestamp, now)}", style="dim")
        head.append(f"  {_ascii_safe(m.account)}", style="dim")

        limit = min(_SNIPPET_MAX, max(20, width - 2))
        body = self._fit(Text(_truncate(_ascii_safe(_clean(m.text)) or "(no text)", limit)))
        if selected:
            head.stylize("reverse")
            body.stylize("reverse")
        _highlight_terms(head, self._find_q)      # after reverse -> matches stay visible
        _highlight_terms(body, self._find_q)

        g.add_row(head)
        g.add_row(body)
        g.add_row(Text(""))
        return g

    def _find_view(self) -> Group:
        width = self._width()
        n = len(self._find_results)
        caret = "_" if self._find_editing else ""
        header = self._fit(Text(
            f" find  {_ascii_safe(self._find_q)}{caret}   "
            f"-  {n} match{'' if n == 1 else 'es'}"
            + (f" (showing {_FIND_MAX})" if n == _FIND_MAX else ""),
            style="bold",
        ))
        parts: list = [header, Text("-" * width, style="grey37")]
        if not n:
            hint = ("start typing to match author + text" if not self._find_q
                    else "no matches")
            parts.append(Text(f"  {hint}", style="dim"))
        else:
            vis = self._visible_cards()
            for i, m in enumerate(self._find_results[self._find_offset:self._find_offset + vis],
                                  start=self._find_offset):
                parts.append(self._result_card(m, selected=(i == self._find_sel), width=width))

        pos = f"{self._find_sel + 1}/{n}" if n else "0/0"
        keys = ("[type] filter   [enter] done   [esc] cancel" if self._find_editing
                else "[j k] move   [enter] open thread   [f] edit   [o] web   [q] back")
        tail = f"[ {self._status} ]" if self._status else keys
        parts.append(self._fit(Text(f" {pos}   {tail}", style="dim")))
        return Group(*parts)

    def _thread_view(self) -> Group:
        t = self.current
        width = self._width()
        head = self._fit(Text())
        head.append("< back    ", style="bold cyan")
        head.append(f"{_ascii_safe(t.account)}  /  ", style="dim")
        head.append(_ascii_safe(_clean(t.channel_name)), style="grey74")

        opts = self.console.options.update(width=width, height=None)
        lines: list = []
        offsets: dict[str, int] = {}
        for mid, rends in self._thread_blocks(t):
            offsets[mid] = len(lines)
            for r in rends:
                lines.extend(self.console.render_lines(r, opts, pad=False))

        if self._scroll_to is not None:                 # arrived here from "find"
            self.scroll = offsets.get(self._scroll_to, self.scroll)
            self._scroll_to = None

        viewport = max(1, self.console.size.height - 3)
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - viewport)))
        shown = lines[self.scroll:self.scroll + viewport]
        segs: list[Segment] = []
        for ln in shown:
            segs.extend(ln)
            segs.append(Segment("\n"))

        last = self.scroll + len(shown)
        keys = "[j k] scroll   [ [  ] ] prev/next   [o] web   [x] read   [f] find   [q] back"
        tail = f"[ {self._status} ]" if self._status else keys
        foot = self._fit(Text(
            f" lines {self.scroll + 1}-{last}/{len(lines)}   {tail}", style="dim",
        ))
        return Group(head, Text("-" * width, style="grey37"), Segments(segs), foot)

    def _thread_blocks(self, t: Thread) -> list[tuple[str, list]]:
        """``[(msg id, [renderables]), ...]`` -- grouped so the thread view can
        map a message id to a scroll offset (for "find" jump-to)."""
        now = datetime.now(timezone.utc)
        mark = (self._thread_mark[1]
                if self._thread_mark and self._thread_mark[0] == t.id else None)
        out: list[tuple[str, list]] = []
        for i, m in enumerate(t.messages):
            rends: list = [] if not i else [Text("")]
            hdr = Text()
            if m.is_mention:
                hdr.append(_MARK_MENTION, style="bold red")
            hdr.append(_ascii_safe(m.author_name) or "(unknown)", style="bold")
            hdr.append(f"   {_relative_time(m.timestamp, now)}", style="dim")
            if m.is_own:
                hdr.append("   you", style="dim italic")
            if i == 0:
                hdr.append("   - thread start", style="dim")
            if m.id == mark:
                hdr.append("   <- found", style="bold yellow")
            rends.append(hdr)
            para = Text(_ascii_safe(_clean(m.text)) or "(no text)", no_wrap=False)
            para.highlight_regex(r"@[\w][\w.\-]*", "cyan")
            para.highlight_regex(r"@(?:channel|here|all|everyone)\b", "bold cyan")
            if m.is_own:
                para.stylize("italic dim")
            if m.id == mark:
                _highlight_terms(para, self._find_q)
            rends.append(para)
            if m.reactions:
                # real emoji here on purpose -- NOT run through _ascii_safe
                rends.append(Text(_reaction_line(m.reactions), style="dim"))
            out.append((m.id, rends))
        return out

    def render(self):
        if self.view == "find":
            return self._find_view()
        if self.view == "thread":
            return self._thread_view()
        return self._list_view()

    # -- input handling --------------------------------------------- #
    def _open(self) -> None:
        if self.current is not None:
            self.view = "thread"
            self.scroll = 0

    def _open_web(self) -> None:
        """Open the selected thread's permalink in the system web browser."""
        t = self.current
        self._open_web_url(t.root.permalink if t else None)

    def _open_web_msg(self, m: Message | None) -> None:
        self._open_web_url(m.permalink if m else None)

    def _open_web_url(self, url: str | None) -> None:
        if not url:
            self._status = "no web link here"
            return
        try:
            opened = webbrowser.open(url, new=2)
        except Exception as exc:                     # no browser / no display
            self._status = f"could not open browser: {exc}"[:70]
            return
        self._status = "opened in browser" if opened else "no browser available"

    def handle(self, ev) -> bool:
        """Return False to quit."""
        self._status = ""            # any keypress clears the transient note

        if self.view == "find":      # find swallows keys (incl. "q", while typing)
            if ev == "CTRL-C":
                return False
            if self._find_editing:
                self._handle_find_edit(ev)
            else:
                self._handle_find(ev)
            return True

        if ev in ("CTRL-C", "q"):
            if self.view == "thread":
                self.view = "list"
                self._thread_mark = None
                return True
            return False
        if ev == "u":
            self._refetch()
            return True
        if ev == "f":
            self._start_find()
            return True

        if self.view == "list":
            self._handle_list(ev)
        else:
            self._handle_thread(ev)
            if self.view == "list":          # left the thread -> drop its mark
                self._thread_mark = None
        return True

    def _handle_list(self, ev) -> None:
        page = self._visible_cards()
        if ev in ("LEFT", "h"):
            self._select_tab(self.tab_idx - 1)
        elif ev in ("RIGHT", "l", "TAB"):
            self._select_tab(self.tab_idx + 1)
        elif ev in ("DOWN", "j"):
            self.sel += 1
        elif ev in ("UP", "k"):
            self.sel -= 1
        elif ev in ("PGDN", " "):
            self.sel += page
        elif ev == "PGUP":
            self.sel -= page
        elif ev in ("HOME", "g"):
            self.sel = 0
        elif ev in ("END", "G"):
            self.sel = len(self.threads) - 1
        elif ev in ("ENTER", "RIGHT"):
            self._open()
        elif ev == "o":
            self._open_web()
        elif ev == "x":
            here = self.sel
            self._mark_read(self.current)
            self.sel = here          # stay put; the list shifts up under us
            self._clamp_list()
        elif ev == "X":
            self._mark_read(list(self.threads))
        elif isinstance(ev, str) and ev.isdigit() and ev != "0":
            self._select_tab(int(ev) - 1)

    def _handle_thread(self, ev) -> None:
        page = max(1, self.console.size.height - 6)
        if ev in ("ESC", "LEFT", "h", "BACKSPACE"):
            self.view = "list"
        elif ev in ("DOWN", "j"):
            self.scroll += 1
        elif ev in ("UP", "k"):
            self.scroll -= 1
        elif ev in ("PGDN", " "):
            self.scroll += page
        elif ev == "PGUP":
            self.scroll -= page
        elif ev in ("HOME", "g"):
            self.scroll = 0
        elif ev in ("END", "G"):
            self.scroll = 10**9
        elif ev in ("]", "J"):
            self.sel = min(self.sel + 1, len(self.threads) - 1)
            self.scroll = 0
        elif ev in ("[", "K"):
            self.sel = max(self.sel - 1, 0)
            self.scroll = 0
        elif ev == "o":
            self._open_web()
        elif ev == "x":
            on_unread_tab = self.tab_idx == 0
            self._mark_read(self.current)
            if on_unread_tab:
                # the thread just left the Unread list; sel now points at the
                # next one (or nothing) -- follow it, or fall back to the list
                self.view = "list" if not self.threads else "thread"
                self.scroll = 0

    # -- loop ------------------------------------------------------- #
    def _paint(self) -> None:
        """Draw exactly one screen-sized frame, anchored at the top-left.

        Rows are emitted at their natural length followed by ``ESC[K`` (erase to
        end of line) rather than space-padded to the full width. That way an
        emoji the terminal renders wider than rich measured (reaction glyphs)
        can't push a line past the edge and wrap the whole frame.
        """
        w, h = self.console.size
        opts = self.console.options.update(width=w, height=h)
        rows = self.console.render_lines(self.render(), opts, pad=False)[:h]
        rows += [[] for _ in range(h - len(rows))]
        chunks = [self.console._render_buffer(iter(ln)).rstrip("\n") for ln in rows]
        out = self.console.file
        out.write("\x1b[H" + "\x1b[K\r\n".join(chunks) + "\x1b[K\x1b[J\x1b[0m")
        out.flush()

    def run(self) -> None:
        if not (sys.stdin.isatty() and self.console.file.isatty()):
            self._dump()
            return
        self._clamp_list()
        with _raw_terminal(self.console) as keys:
            keys.flush()          # ignore whatever was typed before the UI was ready
            self._paint()
            while True:
                ev = keys.next(timeout=0.5)
                if ev is None:
                    continue
                if not self.handle(ev):
                    break
                self._clamp_list()
                self._clamp_find()
                self._paint()

    def _dump(self) -> None:
        for i, name in enumerate(self.tab_names[1:], start=1):
            msgs = [m for t in self.tab_threads[i] for m in t.messages]
            msgs.sort(key=Message.sort_key)
            print_feed(msgs, title=f"{name} - {len(self.tab_threads[i])} threads",
                       console=self.console)


def browse_threads(
    messages: list[Message],
    *,
    console: Console | None = None,
    on_read: Callable[[Thread], None] | None = None,
    on_refetch: Callable[[], list[Message]] | None = None,
) -> None:
    """Open the interactive browser over ``messages`` (grouped into threads).

    ``on_read`` is called once per thread when the user marks it read (``x`` /
    ``X``), for persisting the read marker (server and/or cache). ``on_refetch``
    is called when the user hits ``u`` and must return a fresh message list; the
    browser reloads it in place, keeping the current tab and selection.
    """
    ThreadBrowser(
        messages, console=console, on_read=on_read, on_refetch=on_refetch
    ).run()
