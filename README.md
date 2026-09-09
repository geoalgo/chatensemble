# unichat

Fetch, browse and visualise chat messages from **Mattermost**, **Slack**, and
(later) **Discord** through one API and one CLI. Run `unichat` with no
subcommand for an interactive, multi-account thread browser.

## Install

```sh
uv sync --extra dev
```

## Adding accounts / authentication

One YAML file per account in `~/unichat/accounts/` (git-ignored; override with
`--accounts-dir`). Name = filename; `type` from `type:` or the filename / token
prefix. Values expand `${VAR}` / `$VAR` from the environment. `unichat accounts`
lists what's configured.

**Mattermost** — `~/unichat/accounts/work.yaml`

```yaml
type: mattermost
server: https://mattermost.example.com
username: you@example.com
password: ${MM_PASSWORD}     # or:  token: <personal access token>
# teams: [engineering]       # optional allow-list (default: all)
```

**Slack, app token** — `~/unichat/accounts/slack.yaml`

```yaml
type: slack
token: ${SLACK_TOKEN}        # xoxp- / xoxb-
```

**Slack, no app** — reuse your own browser session (`xoxc-` token + the `xoxd-`
`d` cookie). Pull both from Firefox automatically:

```sh
uv run unichat import-slack --list              # signed-in workspaces (no secrets)
uv run unichat import-slack -w ellis -a ellis   # -> ~/unichat/accounts/ellis.yaml
```

Needs `uv sync --extra slack-cookie` if Slack's stored config is compressed. By
hand instead: DevTools → Network → any `/api/…` call →

```yaml
type: slack
token: ${SLACK_XOXC}         # request's "token" form field
cookie: ${SLACK_XOXD}        # request's "d" cookie
```

## Usage

### Show messages

```sh
uv run unichat accounts                       # list configured accounts
uv run unichat channels --account work        # channels + unread counts

uv run unichat unread                         # unread, dense table
uv run unichat unread --feed                  # unread, feed of cards
uv run unichat fetch --since 7d --channel 'eng-*' --query 'deploy|incident'
uv run unichat fetch --since 3d --feed --lines 2 --width 90

uv run unichat fetch --backfill               # pull all history (see Caching)
uv run unichat cache                          # show what's cached
```

### Browse threads

With **no subcommand**, `unichat` fetches your real accounts and opens an
interactive, `rich`-drawn thread browser:

```sh
uv run unichat                                # fetch (last 21 days), then browse
uv run unichat --days 30 --account work       # narrower window / one account
uv run unichat --no-fetch                     # straight from the cache, no network
uv run unichat --synthetic                    # generated demo data instead
```

A **tab bar** picks the account — `Unread` (first tab) aggregates every unread
thread, `All` merges every account, then one tab per account. The **thread list**
scrolls; opening a thread shows the whole conversation with its own scroll. It's
**keyboard-only** on purpose — mouse reporting stays off so the terminal's own
text selection / copy still works.

| | keys |
|--|--|
| switch account | `← →` / `1`–`9` |
| move / scroll  | `↑ ↓` `j k`, `g` `G`, `PgUp`/`PgDn` |
| open thread    | `⏎` |
| find           | `f` — filter every loaded message by author/text (all terms must match); `⏎` on a hit jumps into its thread |
| open in web browser | `o` — the message permalink (Mattermost / Slack) |
| mark read      | `x` (this thread) · `X` (every thread in the tab) |
| refetch        | `u` — pull fresh data and reload in place |
| in a thread    | `[` `]` prev/next · `o` web · `x` read · `f` find · `q` / `Esc` back |
| quit           | `q` |

Real data goes through the same cache-backed fetch as `fetch`/`unread` (current
month refreshed, `is_unread` from the live read marker), falling back to the
on-disk cache for any account whose fetch fails or that has no config.
`--synthetic` uses `unichat.synthetic.synthetic_messages()` — deterministic
per `--seed`, one *persona* per account (`helmholtz`, `opengpt-x`, `trove-ai`,
each with its own people, channels and topics).

**Mark-read** (`x` / `X`) is kept in a local read-state overlay,
`~/unichat/cache/_read_overlay.json` (thread ids), re-applied on every load
— so it survives a rerun even though a real `fetch` recomputes `is_unread` from
the server (and synthetic data is regenerated). Real mode *also* writes through to
the cache parquet and best-effort advances the server marker (Mattermost
`.../channels/members/me/view`, Slack `conversations.mark`). Clear the overlay
with `unichat cache --clear`. Piping the output (no TTY) prints a
per-account feed instead.

From Python:

```python
from unichat import ChatManager, FetchFilter, print_feed

with ChatManager.from_dir() as mgr:          # ~/unichat/accounts
    msgs = mgr.fetch(FetchFilter(since="7d", unread_only=True))
    print_feed(msgs)
```

## Features

- **Drop-in accounts** — add a YAML file, no code changes; `${VAR}` expansion for secrets.
- **One `fetch(FetchFilter(...))`** across every channel of every account, merged and
  time-sorted. Filters: date range (`since` / `until`, accepts `7d`, `24h`, ISO dates),
  channel globs, account, `unread_only`, `mentions_only`, regex `query`, DM / thread /
  own-message toggles, per-channel and total caps.
- **True per-user unread state** from the server's own read marker; **fetching never
  marks anything read**.
- **Three renderings** — dense table (`--wrap` for full text), a feed of cards (bold
  author, channel badge, relative time, reply count, reactions), and the
  interactive thread browser (no subcommand).
- **Interactive browser** — keyboard-only thread reader across all accounts:
  `Unread` / `All` / per-account tabs, in-place refetch (`u`), mark-read that
  persists across runs (`x` / `X`), open-in-web (`o`).
- **Readable text** — `:emoji:` shortcodes and reactions render as real emoji, markdown
  links collapse to their label, `@mentions` highlighted.
- **Month-partitioned cache, on by default** — past months come from disk, the current
  month is always re-fetched (see below).

### Ask Claude about your messages

The cache is plain Parquet, so a coding agent can answer questions over it directly.
`unichat install-skill` drops a Claude Code skill at `~/.claude/skills/unichat/`
that teaches Claude the schema, a DuckDB query recipe, and how to handle unread
state — so you can ask things like *"summarize the unread messages on WP4 in ufal"*,
*"summarize all unread"*, or *"find where we discussed TRL — when did we pick the
framework?"*.

```sh
uv run unichat install-skill               # -> ~/.claude/skills/unichat/
uv run unichat fetch --since 30d --quiet   # refresh the cache; one line per account, no message dump
```

The skill queries `~/unichat/cache/*/*.parquet` with DuckDB (via `uv run --with
duckdb`, no install needed), refreshes first with `fetch --quiet` when data may be
stale, and excludes threads listed in `~/unichat/cache/_read_overlay.json` (the
browser's manual mark-reads) when reporting what's unread.

## Caching

`fetch` is served through an on-disk cache, one Parquet file (zstd) per account per
calendar month (UTC):

```
~/unichat/cache/<account>/2026-08.parquet   # every message, all channels
                          2026-09.parquet   # current month, rewritten each run
                          _meta.json
```

- **Past months** are read straight from disk; **the current month is always re-fetched**
  and its file overwritten.
- A `fetch` with no `--since`, the **first time** an account is seen, walks history
  backwards a year at a time and stops at the first calendar year with no messages
  (`unread` / `--mentions` never trigger this). Afterwards a plain `fetch` covers only the
  current month; pass `--since` for older months (from cache when present) or `--backfill`
  to walk history again.
- `is_unread` is **recomputed on every online load** from the live read markers, so cached
  months don't go stale on read state.
- `--refresh` re-fetches every month in range; `--no-cache` bypasses disk entirely;
  **`--no-fetch` serves only from cache with no network at all** (offline; `is_unread`
  keeps its stored value).
- `unread` defaults to `--since 90d` to keep the scan bounded.
- `unichat cache` shows coverage; `unichat cache --clear [-a <account>]`
  deletes it (a bare `--clear` also drops the browser's `_read_overlay.json`).

`fetch` / `unread` show a `tqdm` bar per live month (`ufal 2026-07: 73%|███ | 117/160`),
one row per account. The thread browser (startup and `u`) shows a single live
`Fetching from channels [2/5]` line instead. From Python: `mgr.fetch(flt,
progress=True)` for the bars, or pass a `Reporter` — `ChannelProgressReporter(sink)`
for the `[done/total]` line, or a `callable(str)`.

Providers only expose `list_channels()` and `messages_between(start, end, channels)`;
all the per-backend awkwardness (Mattermost has no server-side upper bound, Slack uses
`oldest`/`latest`, …) stays inside the provider.

**Fetch speed.** Two levels of concurrency, each account isolated (own client,
cache dir, token rate limits):

- **Accounts in parallel** — `mgr.fetch` runs one thread per account (`max_workers`,
  default `min(#accounts, 8)`). A 7-day pull over 4 accounts: **19.6s → 6.7s**.
- **Slack channels in parallel** — the per-channel `conversations.history` scan
  runs in a small pool (`concurrency:` in the account YAML, default 6),
  self-throttled under Slack's per-method rate limits so the workers don't provoke
  429s. With the activity probe above, a 14-day pull over ~65 channels: **~10s → ~2s**.

## Providers

| Capability                  | Mattermost | Slack (user token) | Discord |
|-----------------------------|:----------:|:------------------:|:-------:|
| Username / password login   | ✅         | ✖ (no such API)    | ✖       |
| Personal access token       | ✅         | ✅ (`xoxp-`/`xoxb-`) | partial |
| Browser session (as "me")   | —          | ✅ (`xoxc-` + `d` cookie) | ✖  |
| Multi-team / workspace       | ✅         | ✅                 | partial |
| Cross-channel fetch          | ✅         | ✅                 | partial |
| DMs / group DMs / threads    | ✅         | ✅                 | partial |
| True per-user unread state   | ✅         | ✅ (`last_read`)    | partial |
| Mark read (`mark_read`)      | ✅ (channel) | ✅ (`conversations.mark`) | ✖ |

Discord's API terms forbid most user-token automation, so only bot-token access to joined
guilds would be viable — not implemented in this first cut.

## TODOs

- **Reply** - support reply by hitting "r" and typing a message.
- **UI** - better output when loading accounts for first time, show message in case of error. Show "Loading cache from ~/unichat/cache/" when serving from disk. (Scan progress is done: `Fetching from channels [2/5]`, live, on startup and on `u`.)
- **Providers** — Discord (bot token, joined guilds only). Teams.


## Done
- **`import-slack`** — pulls the Slack web token (`xoxc-`) + `d` cookie (`xoxd-`)
  from a local Firefox profile into `~/unichat/accounts/<name>.yaml`; `--list`
  shows signed-in workspaces, `--workspace` picks one. Logic moved from the old
  `scripts/ff_slack_creds.py` into `unichat.slack_cookie` behind the subcommand;
  `cramjam` (for Slack's compressed config blob) is the `slack-cookie` extra.
- **Ask-Claude via the cache** — `unichat install-skill` ships a Claude Code
  skill (`~/.claude/skills/unichat/`) that queries the Parquet cache directly
  (DuckDB over `~/unichat/cache/*/*.parquet`), with the schema, the
  `_read_overlay.json` unread caveat, and worked examples. `fetch --quiet` gives it
  a context-cheap way to refresh first (one line per account, no message table).
  Chose raw-cache + skill over `--json` / an MCP server for now — zero new deps,
  Claude is strong at SQL; MCP stays a later option if Desktop / claude.ai matters.
- **Find (`f`)** — filters every loaded message (across all accounts) whose
  author + text contains all whitespace-separated query terms; live as you type,
  newest first, capped at 1000. `⏎` on a hit switches to that message's account
  tab, opens its thread, scrolls to the message and flags it `<- found`.
- **Browser is the default command** — bare `unichat` fetches the real
  accounts and opens the thread browser (`_cmd_browse`); `--synthetic` swaps in
  generated data, `--no-fetch` reads straight from the cache. (The old `demo`
  subcommand is gone.)
- **Open in web browser (`o`)** — opens the selected thread's permalink
  (`Message.permalink`: Mattermost `/pl/<id>`, Slack `/archives/...`) via
  `webbrowser.open`, from the list or thread view. `⏎` opens the in-terminal
  thread view.
- **Refetch in place (`u`)** — `browse_threads(..., on_refetch=)` re-runs
  `ChatManager.fetch` and reloads the tab list, keeping the current tab (by name)
  and selection (by thread id), after flushing pending mark-reads so the fresh
  data is consistent. A failed or empty fetch keeps the current view.
- **Fetches first** — real mode routes through the cache-backed `ChatManager`
  (current month refreshed, `is_unread` recomputed from live markers), then opens
  the browser. Per-account fetch failure falls back to that account's cache;
  `--no-fetch` / no config reads the cache directly (and still sees disk-only
  accounts).
- **Mark read** — `x` marks the selected thread read, `X` every thread in the tab;
  it leaves `Unread` and its author stops rendering bold immediately (optimistic).
  `browse_threads(..., on_read=)` fires once per thread, batched on `u` / exit
  into a **local read-state overlay** (`~/unichat/cache/_read_overlay.json`,
  thread ids) re-applied on every load, so `x` sticks across reruns regardless of
  the server. Real mode additionally does a cache-parquet write-through and
  best-effort advances the server marker via `ChatManager.mark_read` → provider
  `mark_read` (Mattermost `POST /channels/members/me/view` — channel-scoped;
  Slack `conversations.mark` with the thread's last-activity ts). Clear the
  overlay with `unichat cache --clear`.
- **List unread only** — the thread browser's first tab, `Unread`, aggregates every
  unread thread across all accounts; the browser opens on it when it's non-empty.
- **Synthetic personas** — `--synthetic` generates `helmholtz` / `opengpt-x` /
  `trove-ai`, each with its own people, channel-naming scheme and topics (no
  overlap), deterministic per `--seed`.
- **Cache**
  - Cache files are channel-complete per month, so a first `--backfill` fetches *every*
    channel regardless of `--channel`. Consider a per-`(account, channel, month)` layout
    so filtered fetches are cheap.
  - Past months can still change (edits, deletes, late thread replies); add an age-based
    re-verification instead of trusting them forever.
  - Slack thread replies whose parent is outside the fetched month are missed until that
    month is refreshed.
  - Current-month refresh still issues one request per active channel. Next lever:
    incremental refresh (fetch only since the newest cached message; skips edits/deletes
    to older current-month posts, so `--refresh` stays the full path). Mattermost skips
    channels whose `last_post_at` predates the window with no request at all; Slack now
    does the same via a `client.counts` + `client.userBoot` activity probe (browser
    `xoxc-` tokens only — app tokens fall back to scanning every channel).
  - Parallel-by-month fetch (Slack / Discord / Gmail can partition cleanly).
  - `_meta.json` doesn't record covered date ranges — a month file is all-or-nothing.
