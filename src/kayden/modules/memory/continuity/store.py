import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from kayden.config import CONTINUITY_PATH


class ContinuityStore:
    """Bounded conversation context and explicit durable-memory storage."""

    def __init__(self, path=None):
        self.path = Path(path or CONTINUITY_PATH).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    message_id UNINDEXED,
                    content,
                    tokenize = 'unicode61'
                );

                CREATE TABLE IF NOT EXISTS memories (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS context_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    summary TEXT NOT NULL DEFAULT '',
                    through_message_id INTEGER NOT NULL DEFAULT 0
                );

                INSERT OR IGNORE INTO context_state (id) VALUES (1);
            """)

    def add_message(self, role: str, content: str) -> int:
        if role not in {"user", "assistant"}:
            raise ValueError("role must be user or assistant")
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO messages (role, content) VALUES (?, ?)",
                (role, content),
            )
            message_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO messages_fts (message_id, content) VALUES (?, ?)",
                (message_id, content),
            )
        return message_id

    def messages_after(self, message_id: int = 0) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, role, content, created_at FROM messages "
                "WHERE id > ? ORDER BY id",
                (message_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_messages(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, role, content, created_at FROM ("
                "SELECT id, role, content, created_at FROM messages "
                "ORDER BY id DESC LIMIT ?) ORDER BY id",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        if not tokens:
            raise ValueError("query must contain searchable text")
        return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    def search_messages(self, query: str, limit: int = 10) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        limit = max(1, min(int(limit), 50))
        match = self._fts_query(query)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT m.id, m.role, m.content, m.created_at "
                "FROM messages_fts f JOIN messages m ON m.id = f.message_id "
                "WHERE messages_fts MATCH ? ORDER BY m.id DESC LIMIT ?",
                (match, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def context_state(self) -> tuple[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT summary, through_message_id FROM context_state WHERE id = 1"
            ).fetchone()
        return row["summary"], row["through_message_id"]

    def set_context_state(self, summary: str, through_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE context_state SET summary = ?, through_message_id = ? WHERE id = 1",
                (summary, through_message_id),
            )

    def commit_context_state(
        self,
        expected_through_message_id: int,
        summary: str,
        through_message_id: int,
        compacted_history_messages: int,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE context_state SET summary = ?, through_message_id = ? "
                "WHERE id = 1 AND through_message_id = ?",
                (summary, through_message_id, expected_through_message_id),
            )
            if cursor.rowcount == 1:
                self._prune_compacted_messages(
                    connection,
                    through_message_id,
                    compacted_history_messages,
                )
        return cursor.rowcount == 1

    @staticmethod
    def _prune_compacted_messages(
        connection,
        through_message_id: int,
        keep_recent: int,
    ) -> int:
        """Delete summarized raw messages outside the bounded recall window."""
        keep_recent = max(0, int(keep_recent))
        if keep_recent:
            oldest_retained = connection.execute(
                "SELECT id FROM messages WHERE id <= ? "
                "ORDER BY id DESC LIMIT 1 OFFSET ?",
                (through_message_id, keep_recent - 1),
            ).fetchone()
            if oldest_retained is None:
                return 0
            delete_through = oldest_retained["id"] - 1
        else:
            delete_through = through_message_id

        if delete_through <= 0:
            return 0
        connection.execute(
            "DELETE FROM messages_fts WHERE CAST(message_id AS INTEGER) <= ?",
            (delete_through,),
        )
        cursor = connection.execute(
            "DELETE FROM messages WHERE id <= ?",
            (delete_through,),
        )
        return cursor.rowcount

    def prune_compacted_messages(self, keep_recent: int) -> int:
        """Apply the retention policy to messages already covered by the summary."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT through_message_id FROM context_state WHERE id = 1"
            ).fetchone()
            return self._prune_compacted_messages(
                connection,
                row["through_message_id"],
                keep_recent,
            )

    def latest_message_id(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS id FROM messages"
            ).fetchone()
        return row["id"]

    def remember(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO memories (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = CURRENT_TIMESTAMP",
                (key, value),
            )

    def forget(self, key: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM memories WHERE key = ?", (key,)
            )
        return cursor.rowcount > 0

    def memories(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT key, value, created_at, updated_at FROM memories "
                "ORDER BY key LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
