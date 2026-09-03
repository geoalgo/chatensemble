"""Pretty terminal rendering of messages, via rich."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timezone

from rich.console import Console
from rich.emoji import Emoji
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .models import ChannelKind, Message

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://[^)]+)\)")
_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\((?:[^)]+)\)")
_SHORTCODE_RE = re.compile(r":([a-z0-9_+-]+):")

# Common chat shortcodes that differ from rich's emoji codex.
_EMOJI_ALIASES = {
    "+1": "👍", "-1": "👎", "thumbsup": "👍", "thumbsdown": "👎",
    "tada": "🎉", "pray": "🙏", "rocket": "🚀", "eyes": "👀",
    "white_check_mark": "✅", "heavy_check_mark": "✔️", "x": "❌",
    "raised_hands": "🙌", "wave": "👋", "fire": "🔥", "100": "💯",
    "smile": "😄", "laughing": "😆", "joy": "😂", "sob": "😭",
    "heart": "❤️", "thinking_face": "🤔", "thinking": "🤔",
    "point_up": "☝️", "ok_hand": "👌", "clap": "👏", "muscle": "💪",
    "warning": "⚠️", "bulb": "💡", "bug": "🐛", "sweat_smile": "😅",
    "slightly_smiling_face": "🙂", "sunglasses": "😎",
}


def _emojize(text: str) -> str:
    """Replace :shortcode: tokens with real emoji; leave unknown ones untouched."""
    def sub(m: re.Match[str]) -> str:
        code = m.group(1)
        if code in _EMOJI_ALIASES:
            return _EMOJI_ALIASES[code]
        try:
            return Emoji.replace(f":{code.replace('-', '_')}:")
        except Exception:
            return m.group(0)
    return _SHORTCODE_RE.sub(sub, text)


# Extra console columns consumed by everything that is not the Message column
# (borders + padding + the 5 fixed-ish columns). Used to size a non-TTY console.
_CHROME_COLS = 78

_KIND_MARK = {
    ChannelKind.PUBLIC: "#",
    ChannelKind.PRIVATE: "🔒",
    ChannelKind.DIRECT: "@",
    ChannelKind.GROUP: "👥",
}
_PROVIDER_STYLE = {"mattermost": "cyan", "slack": "magenta", "discord": "blue"}


def _fmt_time(ts: datetime, *, now: datetime | None = None) -> Text:
    now = now or datetime.now(timezone.utc)
    local = ts.astimezone()
    delta = now - ts
    if delta.days == 0 and local.date() == now.astimezone().date():
        label = local.strftime("%H:%M")
    elif delta.days < 7:
        label = local.strftime("%a %H:%M")
    else:
        label = local.strftime("%Y-%m-%d %H:%M")
    return Text(label, style="dim")


def _clean(text: str) -> str:
    text = _MD_IMG_RE.sub(r"[img: \1]", text or "")
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _emojize(text)
    return " ".join(text.split())


def _clip(text: str, width: int) -> str:
    text = _clean(text)
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _relative_time(ts: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    secs = (now - ts).total_seconds()
    future = secs < 0
    secs = abs(secs)

    def unit(n: float, name: str) -> str:
        n = round(n)
        s = f"{n} {name}{'' if n == 1 else 's'}"
        return f"in {s}" if future else f"{s} ago"

    if secs < 45:
        return "just now"
    if secs < 2700:            # < 45 min
        return unit(secs / 60, "minute")
    if secs < 79200:           # < 22 h
        return unit(secs / 3600, "hour")
    if secs < 6 * 86400:
        return unit(secs / 86400, "day")
    if secs < 28 * 86400:
        return unit(secs / (7 * 86400), "week")
    if secs < 335 * 86400:
        return unit(secs / (30 * 86400), "month")
    return unit(secs / (365 * 86400), "year")


def _reaction_summary(reactions: dict[str, int], limit: int = 6) -> str:
    return " ".join(
        _emojize(f":{k}:") + (f"×{v}" if v > 1 else "")
        for k, v in list(reactions.items())[:limit]
    )


def build_table(
    messages: Sequence[Message],
    *,
    title: str | None = None,
    width: int = 80,
    show_account: bool = True,
    group_by_channel: bool = True,
    wrap: bool = False,
) -> Table:
    table = Table(
        title=title,
        title_style="bold",
        expand=False,
        show_lines=wrap,
        header_style="bold dim",
        pad_edge=False,
    )
    table.add_column("", width=1, no_wrap=True)                 # unread / mention marker
    table.add_column("When", no_wrap=True)
    if show_account:
        table.add_column("Account", no_wrap=True, style="dim")
    table.add_column("Channel", no_wrap=True, max_width=22, overflow="ellipsis")
    table.add_column("From", no_wrap=True, max_width=18, overflow="ellipsis")
    table.add_column(
        "Message",
        width=width,
        no_wrap=not wrap,
        overflow="fold" if wrap else "ellipsis",
    )

    now = datetime.now(timezone.utc)
    last_key = None
    for m in messages:
        key = (m.account, m.channel_id)
        if group_by_channel and key != last_key and last_key is not None:
            table.add_section()
        last_key = key

        dot = Text("●", style="yellow") if m.is_unread else Text(" ")
        if m.is_mention:
            dot = Text("★", style="bold red")

        chan = Text(f"{_KIND_MARK.get(m.channel_kind, '')}{_clip(m.channel_name, 20)}")
        author = Text(_clip(m.author_name, 16), style="bold" if m.is_unread else "")
        text_val = _clean(m.text) if wrap else _clip(m.text, width)
        body = Text(text_val or "(no text)")
        if m.is_own:
            body.stylize("italic dim")
        if m.edited:
            body.append(" (edited)", style="dim")
        if m.reactions:
            body.append(f"  {_reaction_summary(m.reactions)}", style="dim")

        row = [dot, _fmt_time(m.timestamp, now=now)]
        if show_account:
            row.append(Text(m.account, style=_PROVIDER_STYLE.get(m.provider, "")))
        row.extend([chan, author, body])
        table.add_row(*row)
    return table


def make_console(width: int = 80) -> Console:
    """A console wide enough for a `width`-wide message column even when piped."""
    con = Console()
    if not con.is_terminal:
        con = Console(width=width + _CHROME_COLS)
    return con


def print_messages(
    messages: Sequence[Message],
    *,
    title: str | None = None,
    console: Console | None = None,
    **kwargs,
) -> None:
    console = console or make_console(kwargs.get("width", 80))
    if not messages:
        console.print("[dim]No messages.[/dim]")
        return
    if title is None:
        unread = sum(1 for m in messages if m.is_unread)
        title = f"{len(messages)} messages" + (f"  ·  {unread} unread" if unread else "")
    console.print(build_table(messages, title=title, **kwargs))


def _pill(m: Message) -> tuple[str, str] | None:
    """(text, style) for the channel badge next to the author, or None to omit."""
    if m.channel_kind == ChannelKind.DIRECT:
        return None                                   # 1:1 — the author already says it
    if m.channel_kind == ChannelKind.GROUP:
        return (f" {_clip(m.channel_name, 44)} ", "grey74 on grey23")
    mark = "🔒 " if m.channel_kind == ChannelKind.PRIVATE else ""
    return (f" {mark}{_clip(m.channel_name, 44)} ", "grey74 on grey23")


def _card(
    m: Message,
    *,
    now: datetime,
    width: int,
    max_lines: int,
    show_account: bool,
    show_links: bool,
) -> Table:
    """One message rendered as a feed card (author · channel-pill · relative time)."""
    card = Table.grid(padding=0)
    card.add_column(width=width)                       # everything wraps at `width`

    head = Table.grid(expand=True, padding=0)
    head.add_column(justify="left", ratio=1)
    head.add_column(justify="right", no_wrap=True)

    left = Text(no_wrap=True, overflow="ellipsis")
    if m.is_mention:
        left.append("★ ", style="bold red")
    elif m.is_unread:
        left.append("● ", style="yellow")
    left.append(m.author_name or "(unknown)", style="bold")
    pill = _pill(m)
    if pill:
        left.append("  ")
        left.append(*pill)
    elif m.channel_kind == ChannelKind.DIRECT:
        left.append("  DM", style="dim")
    if show_account:
        left.append(f"  {m.account}", style=_PROVIDER_STYLE.get(m.provider, "dim"))

    head.add_row(left, Text(" " + _relative_time(m.timestamp, now), style="dim"))
    card.add_row(head)

    body = Text(_clip(m.text, width * max_lines) or "(no text)", no_wrap=False)
    body.highlight_regex(r"@[\w][\w.\-]*", "cyan")
    body.highlight_regex(r"@(?:channel|here|all|everyone)\b", "bold cyan")
    if m.is_own:
        body.stylize("italic dim")
    if m.edited:
        body.append("  (edited)", style="dim italic")
    card.add_row(body)

    foot_bits: list[str] = []
    if m.reply_count:
        foot_bits.append(f"{m.reply_count} repl" + ("y" if m.reply_count == 1 else "ies"))
    if m.reactions:
        foot_bits.append(_reaction_summary(m.reactions))
    if show_links and m.permalink:
        foot_bits.append(m.permalink)
    if foot_bits:
        card.add_row(Text("   ·   ".join(foot_bits), style="dim", no_wrap=True, overflow="ellipsis"))

    card.add_row("")                                   # trailing breathing room
    return card


def build_feed(
    messages: Sequence[Message],
    *,
    now: datetime | None = None,
    width: int = 80,
    max_lines: int = 3,
    show_account: bool = True,
    show_links: bool = False,
) -> Table:
    now = now or datetime.now(timezone.utc)
    outer = Table.grid(padding=0)
    outer.add_column(width=width)
    for i, m in enumerate(messages):
        if i:
            outer.add_row(Rule(style="grey30"))
        outer.add_row(_card(
            m, now=now, width=width, max_lines=max_lines,
            show_account=show_account, show_links=show_links,
        ))
    return outer


def print_feed(
    messages: Sequence[Message],
    *,
    title: str | None = None,
    console: Console | None = None,
    width: int = 80,
    max_lines: int = 3,
    show_account: bool = True,
    show_links: bool = False,
) -> None:
    console = console or make_console(width)
    if not messages:
        console.print("[dim]No messages.[/dim]")
        return
    if title is None:
        unread = sum(1 for m in messages if m.is_unread)
        title = f"{len(messages)} messages" + (f"  ·  {unread} unread" if unread else "")
    header = Table.grid(padding=0)
    header.add_column(width=width)
    header.add_row(Text(title, style="bold"))
    header.add_row(Rule(style="grey30"))
    console.print(header)
    console.print(build_feed(
        messages, now=datetime.now(timezone.utc), width=width, max_lines=max_lines,
        show_account=show_account, show_links=show_links,
    ))


def print_channels(channels, *, console: Console | None = None) -> None:
    console = console or make_console(60)
    table = Table(title=f"{len(channels)} channels", header_style="bold dim", pad_edge=False)
    table.add_column("Account", style="dim")
    table.add_column("Channel")
    table.add_column("Kind", style="dim")
    table.add_column("Team", style="dim")
    table.add_column("Unread", justify="right")
    for ch in sorted(channels, key=lambda c: (c.account, -c.unread_count, c.name.lower())):
        unread = Text(str(ch.unread_count), style="yellow bold") if ch.unread_count else Text("0", style="dim")
        table.add_row(
            ch.account,
            f"{_KIND_MARK.get(ch.kind, '')}{ch.name}",
            ch.kind.value,
            ch.team_name or "",
            unread,
        )
    console.print(table)
