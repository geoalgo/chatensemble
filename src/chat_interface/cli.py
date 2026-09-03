"""Command-line interface: ``chat-interface <command> [options]``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from .config import DEFAULT_ACCOUNTS_DIR, ConfigError
from .display import make_console, print_channels, print_feed, print_messages
from .filters import FetchFilter
from .manager import ChatManager

console = Console()


def _add_filter_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--account", "-a", action="append", dest="accounts",
                   help="restrict to this account (repeatable)")
    p.add_argument("--since", "-s", help="lower time bound: ISO date or offset like 7d, 24h, 2w")
    p.add_argument("--until", "-u", help="upper time bound (same formats as --since)")
    p.add_argument("--channel", "-c", action="append", dest="channels",
                   help="channel name glob, e.g. 'eng-*' (repeatable)")
    p.add_argument("--query", "-q", help="regex matched against message text")
    p.add_argument("--unread", action="store_true", help="only unread messages")
    p.add_argument("--mentions", action="store_true", help="only messages that mention you")
    p.add_argument("--no-dms", action="store_true", help="exclude direct/group messages")
    p.add_argument("--no-threads", action="store_true", help="exclude thread replies")
    p.add_argument("--no-own", action="store_true", help="exclude messages you sent")
    p.add_argument("--limit", type=int, default=500, help="max messages per channel (default 500)")
    p.add_argument("--max", type=int, default=0, dest="max_messages",
                   help="cap on total merged messages (0 = unlimited)")


def _filter_from_args(args: argparse.Namespace) -> FetchFilter:
    return FetchFilter(
        since=args.since,
        until=args.until,
        channels=args.channels,
        accounts=args.accounts,
        unread_only=getattr(args, "unread", False),
        mentions_only=getattr(args, "mentions", False),
        query=args.query,
        include_dms=not args.no_dms,
        include_threads=not args.no_threads,
        include_own=not args.no_own,
        limit_per_channel=args.limit,
        max_messages=args.max_messages,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chat-interface",
        description="With no subcommand: fetch and open the interactive thread browser.",
    )
    parser.add_argument("--accounts-dir", "-d", default=str(DEFAULT_ACCOUNTS_DIR),
                        help=f"directory of account YAML files (default: {DEFAULT_ACCOUNTS_DIR})")
    # options for the default (no-subcommand) thread browser
    parser.add_argument("--account", "-a", action="append", dest="accounts",
                        help="restrict to this account (repeatable)")
    parser.add_argument("--synthetic", action="store_true",
                        help="use generated demo data instead of real accounts")
    parser.add_argument("--no-fetch", action="store_true",
                        help="skip the network; browse straight from the on-disk cache")
    parser.add_argument("--days", type=int, default=21,
                        help="lookback window (real), or spread of synthetic threads (default 21)")
    parser.add_argument("--no-tui", action="store_true",
                        help="print a per-account feed instead of the interactive viewer")
    parser.add_argument("--seed", type=int, default=0,
                        help="--synthetic: RNG seed (default 0)")
    parser.add_argument("--threads", type=int, default=14,
                        help="--synthetic: threads per account (default 14)")
    sub = parser.add_subparsers(dest="command", required=False)

    sub.add_parser("accounts", help="list configured accounts")

    p_ch = sub.add_parser("channels", help="list channels and unread counts")
    p_ch.add_argument("--account", "-a", action="append", dest="accounts")

    for name, helptext in (
        ("fetch", "fetch and display messages"),
        ("unread", "show unread messages (shortcut for fetch --unread)"),
    ):
        sp = sub.add_parser(name, help=helptext)
        _add_filter_args(sp)
        sp.add_argument("--no-account-col", action="store_true", help="hide the account column")
        sp.add_argument("--width", type=int, default=80, help="message / card text width")
        sp.add_argument("--wrap", action="store_true",
                        help="table view: show full message text over multiple lines")
        sp.add_argument("--feed", action="store_true",
                        help="render as a scrollable feed of cards instead of a table")
        sp.add_argument("--lines", type=int, default=3,
                        help="feed view: max text lines per message (default 3)")
        sp.add_argument("--links", action="store_true",
                        help="feed view: also show each message's permalink")
        sp.add_argument("--no-cache", action="store_true",
                        help="bypass the on-disk cache entirely")
        sp.add_argument("--no-fetch", action="store_true",
                        help="serve only from cache; no network at all")
        sp.add_argument("--backfill", action="store_true",
                        help="walk history back a year at a time until an empty year")
        sp.add_argument("--refresh", action="store_true",
                        help="re-fetch every month in range, ignoring cached files")

    p_cache = sub.add_parser("cache", help="inspect or clear the message cache")
    p_cache.add_argument("--account", "-a", help="limit to this account")
    p_cache.add_argument("--clear", action="store_true", help="delete cached files")
    return parser


def _cmd_accounts(mgr: ChatManager) -> None:
    from rich.table import Table

    t = Table(title=f"{len(mgr.clients)} accounts", header_style="bold dim")
    t.add_column("Name")
    t.add_column("Type", style="dim")
    t.add_column("Endpoint", style="dim")
    for c in mgr.clients:
        endpoint = getattr(c, "server", "") or "slack.com"
        t.add_row(c.name, c.provider, endpoint)
    console.print(t)


def _cmd_cache(mgr: ChatManager, args: argparse.Namespace) -> None:
    from rich.table import Table

    if args.clear:
        n = mgr.cache_clear(args.account)
        if not args.account:
            p = _overlay_path(mgr.cache_root)
            if p.is_file():
                p.unlink()
                n += 1
        console.print(f"removed {n} cached file(s)")
        return

    status = mgr.cache_status()
    t = Table(title="message cache", header_style="bold dim", pad_edge=False)
    t.add_column("Account")
    t.add_column("Month")
    t.add_column("Messages", justify="right")
    any_rows = False
    for account, months in status.items():
        if args.account and account != args.account:
            continue
        for month, count in months.items():
            t.add_row(account, month, "?" if count is None else str(count))
            any_rows = True
    if not any_rows:
        console.print("[dim]cache is empty[/dim]")
    else:
        console.print(t)


def _overlay_path(cache_root):
    return Path(cache_root) / "_read_overlay.json"


def _load_read_overlay(cache_root) -> set[str]:
    """Thread ids the user has marked read in the browser.

    A local read-state overlay, re-applied on every load: synthetic data is
    regenerated each run, and a real ``fetch`` recomputes ``is_unread`` from the
    server marker (which may not have moved, e.g. thread-level reads on a
    collapsed-threads server). The overlay keeps ``x`` sticky regardless.
    """
    import json

    p = _overlay_path(cache_root)
    if not p.is_file():
        return set()
    try:
        return set(json.loads(p.read_text()).get("threads", []))
    except (json.JSONDecodeError, OSError, AttributeError):
        return set()


def _save_read_overlay(cache_root, thread_ids: set[str]) -> None:
    import json

    p = _overlay_path(cache_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"threads": sorted(thread_ids)}, indent=0))


def _apply_read_overlay(messages, thread_ids: set[str]) -> None:
    if not thread_ids:
        return
    for m in messages:
        if (m.thread_id or m.id) in thread_ids:
            m.is_unread = m.is_mention = False


def _cached_accounts(cache_root) -> list[str]:
    root = Path(cache_root)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "_meta.json").is_file()
                  or any(p.glob("*.parquet")))


def _persist_read(cache_root, threads) -> None:
    """Write ``is_unread = False`` through to the affected cache month files.

    This is the offline half of "mark read": the next *online* fetch recomputes
    ``is_unread`` from the live server marker anyway, so if the server disagrees
    this will be corrected then.
    """
    from .cache import MonthStore, month_key

    per_month: dict[tuple[str, str], set[str]] = {}
    for t in threads:
        for m in t.messages:
            per_month.setdefault((m.account, month_key(m.timestamp)), set()).add(m.id)

    for (account, key), ids in per_month.items():
        store = MonthStore(cache_root, account)
        if not store.has(key):
            continue
        msgs = store.load(key)
        touched = False
        for m in msgs:
            if m.id in ids and m.is_unread:
                m.is_unread = False
                touched = True
        if touched:
            store.save(key, msgs)


def _read_cache_direct(cache_root, days: int, accounts=None):
    """Every cached message across all cache dirs, newer than ``days`` ago.

    Used when there is no accounts config, or for ``--no-fetch``. Unlike a fetch
    it sees accounts that only exist on disk.
    """
    from datetime import datetime, timezone

    from .cache import MonthStore

    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    want = set(accounts) if accounts else None
    out = []
    for name in _cached_accounts(cache_root):
        if want and name not in want:
            continue
        store = MonthStore(cache_root, name)
        for key in store.months():
            out += [m for m in store.load(key) if m.timestamp.timestamp() >= cutoff]
    return out


def _server_mark_read(mgr, threads) -> list[tuple[str, str]]:
    """Advance the server read marker once per (account, channel) touched.

    Marks each channel read up to the latest ``last_activity`` among the threads
    the user marked read in it. Returns ``[(account, error), ...]``.
    """
    latest: dict[tuple[str, str], object] = {}
    for t in threads:
        key = (t.account, t.root.channel_id)
        if key not in latest or t.last_activity > latest[key]:
            latest[key] = t.last_activity
    errors: list[tuple[str, str]] = []
    for (account, channel_id), up_to in latest.items():
        try:
            mgr.mark_read(account, channel_id, up_to=up_to)
        except Exception as exc:  # provider/network/NotImplemented -- keep going
            errors.append((account, str(exc)))
    return errors


def _cmd_browse(args: argparse.Namespace) -> None:
    from .browser import browse_threads
    from .cache import DEFAULT_CACHE_ROOT
    from .synthetic import synthetic_messages

    real = not args.synthetic
    cache_root = DEFAULT_CACHE_ROOT
    mgr = None
    flt = FetchFilter(since=f"{args.days}d", accounts=args.accounts) if real else None

    if not real:
        messages = synthetic_messages(
            args.accounts, seed=args.seed,
            threads_per_account=args.threads, days=args.days,
        )
    else:
        try:
            mgr = ChatManager.from_dir(args.accounts_dir)
        except ConfigError as exc:
            console.print(f"[yellow]no usable accounts config ({exc}); cache only[/yellow]")

        if mgr is None or args.no_fetch:
            messages = _read_cache_direct(cache_root, args.days, args.accounts)
        else:
            messages = mgr.fetch(flt, progress=sys.stderr.isatty())
            for name, err in mgr.errors.items():
                console.print(f"[yellow]{name}: {err} — showing cached data[/yellow]")
                messages += _read_cache_direct(cache_root, args.days, [name])

    live = mgr is not None and not args.no_fetch
    overlay = _load_read_overlay(cache_root)
    _apply_read_overlay(messages, overlay)

    if not messages:
        hint = ("try --no-fetch, or run a fetch first" if real
                else "check --account / --threads")
        console.print(f"[dim]nothing to show — {hint}[/dim]")
        if mgr is not None:
            mgr.close()
        return

    messages.sort(key=lambda m: (m.timestamp, m.id))

    try:
        if args.no_tui:
            from .threads import group_threads
            threads = group_threads(messages)
            by_acct: dict[str, list] = {}
            for t in threads:
                by_acct.setdefault(t.account, []).extend(t.messages)
            for acct, msgs in by_acct.items():
                msgs.sort(key=lambda m: (m.timestamp, m.id))
                n = sum(1 for t in threads if t.account == acct)
                print_feed(msgs, title=f"{acct} - {n} threads")
            return

        # mark-read: flushed on "u" (before a refetch) and again on exit --
        # cache write-through always, plus the live server marker when fetching.
        marked: list = []
        sync_errors: list[tuple[str, str]] = []
        total_read = 0

        def flush_read() -> None:
            nonlocal total_read
            if not marked:
                return
            total_read += len(marked)
            overlay.update(t.id for t in marked)          # local read-state overlay
            _save_read_overlay(cache_root, overlay)
            if real:
                _persist_read(cache_root, marked)         # + parquet write-through
                if live:                                  # + best-effort server marker
                    sync_errors.extend(_server_mark_read(mgr, marked))
            marked.clear()

        def refetch() -> list:
            flush_read()                      # commit reads before pulling fresh state
            fresh = mgr.fetch(flt, progress=False)
            sync_errors.extend((n, f"refetch: {e}") for n, e in mgr.errors.items())
            _apply_read_overlay(fresh, overlay)
            return fresh

        browse_threads(
            messages, console=Console(),
            on_read=marked.append,
            on_refetch=(refetch if live else None),
        )
        flush_read()

        for account, err in sync_errors:
            console.print(f"[yellow]{account}: {err}[/yellow]")
        if total_read:
            console.print(f"[dim]marked {total_read} thread(s) read[/dim]")
    finally:
        if mgr is not None:
            mgr.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command is None:
        _cmd_browse(args)
        return 0

    try:
        mgr = ChatManager.from_dir(args.accounts_dir)
    except ConfigError as exc:
        console.print(f"[red]config error:[/red] {exc}")
        return 2

    try:
        if args.command == "accounts":
            _cmd_accounts(mgr)
        elif args.command == "channels":
            flt = FetchFilter(accounts=args.accounts)
            print_channels(mgr.channels(flt), console=console)
        elif args.command == "cache":
            _cmd_cache(mgr, args)
        elif args.command in ("fetch", "unread"):
            flt = _filter_from_args(args)
            if args.command == "unread":
                flt.unread_only = True
                if flt.since is None:
                    flt.since = "90d"          # unread is recent; keep it bounded
            if args.no_fetch and args.no_cache:
                console.print("[yellow]--no-fetch overrides --no-cache[/yellow]")
            messages = mgr.fetch(
                flt,
                cache=not args.no_cache,
                backfill=args.backfill,
                refresh=args.refresh,
                offline=args.no_fetch,
                progress=not args.no_fetch and sys.stderr.isatty(),
            )
            out_console = make_console(args.width)
            if args.feed:
                print_feed(
                    messages,
                    console=out_console,
                    width=args.width,
                    max_lines=args.lines,
                    show_account=not args.no_account_col,
                    show_links=args.links,
                )
            else:
                print_messages(
                    messages,
                    console=out_console,
                    width=args.width,
                    wrap=args.wrap,
                    show_account=not args.no_account_col,
                )
            for name, err in getattr(mgr, "errors", {}).items():
                console.print(f"[red]{name}:[/red] {err}")
        return 0
    finally:
        mgr.close()


if __name__ == "__main__":
    sys.exit(main())
