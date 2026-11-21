"""SQLite persistence for sessions and messages (stdlib sqlite3, no ORM)."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    system_prompt TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    metadata   TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:  # commits on success, rolls back on exception
                yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

    def ping(self) -> bool:
        try:
            with self.connection() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()

    def session_exists(self, session_id: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? UNION SELECT 1 FROM messages WHERE session_id = ? LIMIT 1",
                (session_id, session_id),
            ).fetchone()
        return row is not None

    def get_history(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Messages for a session, oldest first. `limit` keeps only the most recent N."""
        with self.connection() as conn:
            if limit:
                rows = conn.execute(
                    "SELECT * FROM (SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC", (session_id,)
                ).fetchall()
        history = []
        for row in rows:
            item: dict[str, Any] = {"role": row["role"], "content": row["content"]}
            if row["role"] == "assistant" and row["metadata"]:
                item["metadata"] = json.loads(row["metadata"])
            item["created_at"] = row["created_at"]
            history.append(item)
        return history

    def save_turn(
        self,
        session_id: str,
        system_prompt: str | None,
        user_content: str,
        assistant_content: str,
        metadata: dict[str, Any],
    ) -> None:
        """Persist a successful exchange atomically: session upsert + user + assistant rows."""
        now = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, system_prompt, created_at, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    system_prompt = excluded.system_prompt,
                    updated_at = excluded.updated_at
                """,
                (session_id, system_prompt, now, now),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, metadata, created_at) VALUES (?, 'user', ?, NULL, ?)",
                (session_id, user_content, now),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, metadata, created_at) VALUES (?, 'assistant', ?, ?, ?)",
                (session_id, assistant_content, json.dumps(metadata), utc_now()),
            )
