---
name: chatensemble
description: >
  Answer questions about the user's chat messages (Mattermost / Slack) by querying
  the local `chatensemble` message cache. Use for: summarize unread messages
  (optionally scoped to an account or channel), search across chats for a topic,
  recall a decision or discussion from a channel/thread, "what did <person> say
  about <X>", "when did we choose <Y>".
---

# chatensemble — query the local chat cache

`chatensemble` stores every fetched message as **Parquet**, one flat row per
message:

```
~/chatensemble/cache/<account>/<YYYY-MM>.parquet     # month-partitioned, UTC
~/chatensemble/cache/_read_overlay.json              # thread ids marked read in the browser
```

## Step 1 — refresh if stale

The cache is only as current as the last fetch. If the user hasn't run
`chatensemble` (fetch or the browser) in the last hour or two, refresh first —
it prints one line per account, no message dump:

```sh
chatensemble fetch --since 30d --quiet
# scope it:  chatensemble fetch --since 30d --quiet -a ufal
# older discussion? widen it: chatensemble fetch --since 6m --quiet
```

Skip this if a fetch/browse clearly ran recently, or the user says to use the cache as-is.

To map a vague channel name ("WP4") to a real `channel_name`:

```sh
chatensemble channels -a ufal
```

## Step 2 — query the Parquet with DuckDB

Run DuckDB via `uv` (no install needed — it's fetched once and cached):

````sh
uv run --with duckdb python - <<'PY'
import duckdb, os
root = os.path.expanduser("~/chatensemble/cache")
duckdb.sql(f"""
  SELECT m.account, m.channel_name, m.author_name,
         strftime(epoch_ms(m.ts_ms), '%Y-%m-%d %H:%M') AS at,
         m.text, m.thread_id, m.permalink
  FROM read_parquet('{root}/*/*.parquet') m
  WHERE m.is_unread
    AND lower(m.channel_name) LIKE '%wp4%'
  ORDER BY m.ts_ms
""").show(max_rows=200)
PY
````

- Always `WHERE`-filter and select only the columns you need — never `SELECT *` a
  whole month.
- Use `.pl()` / `.df()` / `.fetchall()` instead of `.show()` if you need to
  process the rows in Python.
- If the `duckdb` CLI is already on PATH, `duckdb -json -c "<same SQL>"` also works
  (`read_parquet('~/chatensemble/cache/*/*.parquet')`).
- Last resort (no network for `uv`): `pyarrow` ships with `chatensemble` — read
  files with `pyarrow.parquet.read_table(f, columns=[...])` and filter in Python.

## Schema

| column | notes |
|---|---|
| `id` | message id |
| `text` | body (Slack is pre-cleaned; Mattermost keeps raw markdown) |
| `ts_ms` | epoch **milliseconds**, UTC → `epoch_ms(ts_ms)` in DuckDB |
| `author_name`, `author_id` | display name / stable id |
| `channel_name`, `channel_id` | for a DM, `channel_name` is the other person |
| `channel_kind` | `public` \| `private` \| `direct` \| `group` |
| `account`, `provider` | account name; `mattermost` \| `slack` |
| `thread_id` | the thread's root id; **equals `id` for a root / standalone message** |
| `reply_count`, `is_mention`, `is_own`, `edited` | |
| `is_unread` | **see caveat below** |
| `permalink` | web URL to the message |
| `reactions_json` | JSON string, `{emoji_name: count}` |
| `raw_json` | provider payload — ignore |

## `is_unread` caveat

`is_unread` is the value from the **last fetch** — accurate right after
`chatensemble fetch` (recomputed from the server read marker). The interactive
browser's manual "mark read" (`x`) is stored **only** in
`~/chatensemble/cache/_read_overlay.json` as `{"threads": ["<thread_id>", ...]}`, never
written back to Parquet.

So for "what's unread": refresh first (Step 1) **and** exclude overlay threads:

```sql
WITH read AS (
  SELECT unnest(threads) AS thread_id
  FROM read_json_auto('~/chatensemble/cache/_read_overlay.json')
)
SELECT m.account, m.channel_name, m.author_name,
       strftime(epoch_ms(m.ts_ms), '%Y-%m-%d %H:%M') AS at, m.text, m.permalink
FROM read_parquet('~/chatensemble/cache/*/*.parquet') m
WHERE m.is_unread
  AND m.thread_id NOT IN (SELECT thread_id FROM read)
ORDER BY m.account, m.ts_ms
```

(expand `~` to `os.path.expanduser("~")` inside the Python `f"""..."""`.)

## Worked examples

### "Summarize the unread messages on WP4 / ufal"
1. `chatensemble fetch --since 30d --quiet -a ufal`
2. Run the unread query above with `read_parquet('{root}/ufal/*.parquet')` and
   `AND lower(m.channel_name) LIKE '%wp4%'`.
3. Read the rows chronologically; summarize grouped by thread / sub-topic; cite
   `permalink`s for anything worth opening.

### "Summarize all unread messages"
Same query, no channel/account filter. If it's a lot, summarize grouped by
`account` then `channel_name` (a line or two each) with a total count.

### "Find all messages where we discussed TRL — when did we choose the framework?"
1. Refresh with a wide window if it may be old: `chatensemble fetch --since 6m --quiet`.
2. Find matching threads:
   `SELECT DISTINCT account, thread_id, channel_name FROM read_parquet('{root}/*/*.parquet') WHERE text ILIKE '%TRL%'`
3. Pull each full thread in order:
   `SELECT author_name, strftime(epoch_ms(ts_ms),'%Y-%m-%d %H:%M') at, text, permalink
    FROM read_parquet('{root}/*/*.parquet')
    WHERE thread_id = '<id>' AND account = '<account>' ORDER BY ts_ms`
4. Answer: quote the message(s) where the framework was chosen, with the date and
   `permalink`.
