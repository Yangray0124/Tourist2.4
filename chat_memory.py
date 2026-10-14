"""Per-channel Discord memory: recent text, compressed archives and local BM25 retrieval."""

import json
import re
import sqlite3
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

RECENT_MESSAGE_LIMIT = 20


def search_tokens(text: str) -> str:
    """Make Chinese bigrams and Latin words searchable with SQLite FTS5."""
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            tokens.append(run)
    return " ".join(dict.fromkeys(tokens))


class ChatMemory:
    """Connections are short lived so callers can safely use asyncio.to_thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
                    author TEXT NOT NULL, created_at TEXT NOT NULL,
                    content TEXT NOT NULL, important INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS messages_channel ON messages(channel_id, id);
                CREATE TABLE IF NOT EXISTS captured_ids (id INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO captured_ids SELECT id FROM messages;
                CREATE TABLE IF NOT EXISTS voice_replies (
                    trigger_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL, content TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS voice_replies_channel ON voice_replies(channel_id, trigger_id);
                CREATE TABLE IF NOT EXISTS archives (
                    id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
                    first_id INTEGER NOT NULL, last_id INTEGER NOT NULL,
                    summary TEXT NOT NULL, original BLOB NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5(
                    channel_id UNINDEXED, kind UNINDEXED, source_id UNINDEXED,
                    last_id UNINDEXED, text UNINDEXED, tokens
                );
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(
        self, message_id: int, channel_id: int, author: str, created_at: str, content: str
    ) -> None:
        with self.connect() as db:
            seen = db.execute("INSERT OR IGNORE INTO captured_ids VALUES (?)", (message_id,))
            if not seen.rowcount:
                return
            cursor = db.execute(
                "INSERT OR IGNORE INTO messages(id, channel_id, author, created_at, content) "
                "VALUES (?, ?, ?, ?, ?)",
                (message_id, channel_id, author, created_at, content),
            )
            if cursor.rowcount:
                text = f"[{created_at}] {author}: {content}"
                db.execute(
                    "INSERT INTO memory_search VALUES (?, 'original', ?, ?, ?, ?)",
                    (channel_id, message_id, message_id, text, search_tokens(content)),
                )

    def record_voice_reply(self, trigger_id: int, channel_id: int, created_at: str, content: str) -> None:
        """Keep the spoken answer privately so future turns can refer to it."""
        if not content.strip():
            return
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO voice_replies VALUES (?, ?, ?, ?)",
                (trigger_id, channel_id, created_at, content),
            )
            if cursor.rowcount:
                text = f"[{created_at}] Tourist 語音：{content}"
                db.execute(
                    "INSERT INTO memory_search VALUES (?, 'voice', ?, ?, ?, ?)",
                    (channel_id, trigger_id, trigger_id, text, search_tokens(content)),
                )

    def voice_history(self, channel_id: int, before_id: int) -> str:
        with self.connect() as db:
            rows = db.execute(
                "SELECT created_at, content FROM voice_replies WHERE channel_id = ? AND trigger_id < ? "
                "ORDER BY trigger_id DESC LIMIT ?", (channel_id, before_id, RECENT_MESSAGE_LIMIT)
            ).fetchall()
        lines: list[str] = []
        remaining = 12000
        for row in rows:
            line = f"[{row['created_at']}] Tourist 語音：{row['content']}"
            if len(line) > remaining:
                if not lines:
                    lines.append(line[:remaining])
                break
            lines.append(line)
            remaining -= len(line) + 1
        return "\n".join(reversed(lines))

    def pending_batches(self, channel_id: int | None = None) -> list[list[dict[str, Any]]]:
        """Keep the recent window verbatim; summarize up to 100 older messages."""
        batches: list[list[dict[str, Any]]] = []
        with self.connect() as db:
            channels = db.execute(
                "SELECT DISTINCT channel_id FROM messages WHERE ? IS NULL OR channel_id = ?",
                (channel_id, channel_id),
            ).fetchall()
            for channel in channels:
                rows = db.execute(
                    "SELECT * FROM messages WHERE channel_id = ? AND important = 0 "
                    "AND id < COALESCE((SELECT id FROM messages WHERE channel_id = ? "
                    "ORDER BY id DESC LIMIT 1 OFFSET ?), 0) ORDER BY id LIMIT 100",
                    (channel[0], channel[0], RECENT_MESSAGE_LIMIT - 1),
                ).fetchall()
                if len(rows) >= 30:
                    batches.append([dict(row) for row in rows])
        return batches

    def archive(self, rows: list[dict[str, Any]], summary: str, important_ids: set[int]) -> None:
        """Commit archive, retained originals and retrieval index atomically."""
        if not summary.strip():
            raise ValueError("Empty memory summary")
        channel_id = rows[0]["channel_id"]
        packed = zlib.compress(json.dumps(rows, ensure_ascii=False).encode("utf-8"))
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO archives(channel_id, first_id, last_id, summary, original) "
                "VALUES (?, ?, ?, ?, ?)",
                (channel_id, rows[0]["id"], rows[-1]["id"], summary, packed),
            )
            db.execute(
                "INSERT INTO memory_search VALUES (?, 'summary', ?, ?, ?, ?)",
                (channel_id, cursor.lastrowid, rows[-1]["id"], summary, search_tokens(summary)),
            )
            for row in rows:
                if row["id"] in important_ids:
                    db.execute("UPDATE messages SET important = 1 WHERE id = ?", (row["id"],))
                else:
                    db.execute("DELETE FROM messages WHERE id = ?", (row["id"],))
                    db.execute(
                        "DELETE FROM memory_search WHERE kind = 'original' AND source_id = ?",
                        (row["id"],),
                    )

    def search(self, channel_id: int, query: str, before_id: int) -> str:
        tokens = search_tokens(re.sub(r"<[@#][!&]?\d+>", "", query)).split()[:32]
        if not tokens:
            return ""
        match = " OR ".join(f'"{token}"' for token in tokens)
        with self.connect() as db:
            recent = db.execute(
                "SELECT id FROM messages WHERE channel_id = ? AND id < ? "
                "ORDER BY id DESC LIMIT 1 OFFSET ?", (channel_id, before_id, RECENT_MESSAGE_LIMIT - 1)
            ).fetchone()
            cutoff = recent[0] if recent else before_id
            rows = db.execute(
                "SELECT kind, text FROM memory_search WHERE memory_search MATCH ? "
                "AND channel_id = ? AND CAST(last_id AS INTEGER) < ? "
                "AND (kind = 'summary' OR CAST(last_id AS INTEGER) < ?) "
                "ORDER BY bm25(memory_search) LIMIT 6",
                (match, channel_id, before_id, cutoff),
            ).fetchall()
        return "\n\n".join(f"[{row['kind']}] {row['text']}" for row in rows)[:10000]
