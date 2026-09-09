"""Message <-> Parquet for the on-disk cache.

One row per message, flat schema; the provider-specific ``raw`` payload and the
``reactions`` mapping are carried as JSON strings so the schema stays stable
across providers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .models import ChannelKind, Message, from_epoch_ms

SCHEMA_VERSION = 1

_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("text", pa.string()),
        ("ts_ms", pa.int64()),
        ("author_id", pa.string()),
        ("author_name", pa.string()),
        ("channel_id", pa.string()),
        ("channel_name", pa.string()),
        ("channel_kind", pa.string()),
        ("account", pa.string()),
        ("provider", pa.string()),
        ("thread_id", pa.string()),
        ("reply_count", pa.int64()),
        ("is_unread", pa.bool_()),
        ("is_mention", pa.bool_()),
        ("is_own", pa.bool_()),
        ("edited", pa.bool_()),
        ("permalink", pa.string()),
        ("reactions_json", pa.string()),
        ("raw_json", pa.string()),
    ],
    metadata={b"unichat.schema": str(SCHEMA_VERSION).encode()},
)

_COLUMNS = [f.name for f in _SCHEMA]


def message_to_row(m: Message) -> dict:
    return {
        "id": m.id,
        "text": m.text,
        "ts_ms": int(m.timestamp.timestamp() * 1000),
        "author_id": m.author_id,
        "author_name": m.author_name,
        "channel_id": m.channel_id,
        "channel_name": m.channel_name,
        "channel_kind": m.channel_kind.value,
        "account": m.account,
        "provider": m.provider,
        "thread_id": m.thread_id,
        "reply_count": m.reply_count,
        "is_unread": m.is_unread,
        "is_mention": m.is_mention,
        "is_own": m.is_own,
        "edited": m.edited,
        "permalink": m.permalink,
        "reactions_json": json.dumps(m.reactions, ensure_ascii=False),
        "raw_json": json.dumps(m.raw, ensure_ascii=False, default=str),
    }


def message_from_row(d: dict) -> Message:
    return Message(
        id=d["id"],
        text=d["text"] or "",
        timestamp=from_epoch_ms(d["ts_ms"]),
        author_id=d["author_id"] or "",
        author_name=d["author_name"] or "",
        channel_id=d["channel_id"] or "",
        channel_name=d["channel_name"] or "",
        channel_kind=ChannelKind(d["channel_kind"] or "unknown"),
        account=d["account"] or "",
        provider=d["provider"] or "",
        thread_id=d["thread_id"],
        reply_count=d["reply_count"] or 0,
        is_unread=bool(d["is_unread"]),
        is_mention=bool(d["is_mention"]),
        is_own=bool(d["is_own"]),
        edited=bool(d["edited"]),
        permalink=d["permalink"],
        reactions=json.loads(d["reactions_json"]) if d.get("reactions_json") else {},
        raw=json.loads(d["raw_json"]) if d.get("raw_json") else {},
    )


def write_parquet(path: Path, messages: list[Message]) -> None:
    rows = [message_to_row(m) for m in messages]
    cols = {name: [r[name] for r in rows] for name in _COLUMNS}
    table = pa.table(cols, schema=_SCHEMA)
    pq.write_table(table, path, compression="zstd")


def read_parquet(path: Path) -> list[Message]:
    table = pq.read_table(path)
    return [message_from_row(r) for r in table.to_pylist()]
