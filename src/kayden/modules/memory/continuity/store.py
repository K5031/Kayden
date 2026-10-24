import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from kayden.config import CONTINUITY_PATH


class ContinuityStore:
    """Active transcript, bounded memory, and historical-summary archive."""

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

                CREATE TABLE IF NOT EXISTS memory_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    document TEXT NOT NULL DEFAULT '',
                    migrated_legacy INTEGER NOT NULL DEFAULT 0
                );

                INSERT OR IGNORE INTO context_state (id) VALUES (1);
                INSERT OR IGNORE INTO memory_state (id) VALUES (1);

                CREATE TABLE IF NOT EXISTS historical_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_message_id INTEGER NOT NULL,
                    through_message_id INTEGER NOT NULL UNIQUE,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS historical_summaries_fts
                USING fts5(
                    summary,
                    archive_id UNINDEXED,
                    tokenize = 'porter unicode61'
                );

                DROP TABLE IF EXISTS messages_fts;
            """)
            state = connection.execute(
                "SELECT document, migrated_legacy FROM memory_state WHERE id = 1"
            ).fetchone()
            if not state["migrated_legacy"]:
                legacy = connection.execute(
                    "SELECT key, value FROM memories ORDER BY key"
                ).fetchall()
                additions = [f"- {row['key']}: {row['value']}" for row in legacy]
                document = state["document"].strip()
                if additions:
                    document = "\n".join(filter(None, [document, *additions]))
                connection.execute(
                    "UPDATE memory_state SET document = ?, migrated_legacy = 1 "
                    "WHERE id = 1",
                    (document,),
                )
                connection.execute("DELETE FROM memories")

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
        return cursor.lastrowid

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

    def context_state(self) -> tuple[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT summary, through_message_id FROM context_state WHERE id = 1"
            ).fetchone()
        return row["summary"], row["through_message_id"]

    def commit_context_state(
        self,
        expected_through_message_id: int,
        summary: str,
        memory_document: str,
        historical_summary: str,
        through_message_id: int,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE context_state SET summary = ?, through_message_id = ? "
                "WHERE id = 1 AND through_message_id = ?",
                (summary, through_message_id, expected_through_message_id),
            )
            if cursor.rowcount == 1:
                archive = connection.execute(
                    "INSERT INTO historical_summaries "
                    "(from_message_id, through_message_id, summary) "
                    "VALUES (?, ?, ?)",
                    (
                        expected_through_message_id + 1,
                        through_message_id,
                        historical_summary,
                    ),
                )
                connection.execute(
                    "INSERT INTO historical_summaries_fts "
                    "(summary, archive_id) VALUES (?, ?)",
                    (historical_summary, archive.lastrowid),
                )
                connection.execute(
                    "UPDATE memory_state SET document = ? WHERE id = 1",
                    (memory_document,),
                )
                self._delete_messages_through(connection, through_message_id)
        return cursor.rowcount == 1

    def historical_summaries(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, from_message_id, through_message_id, summary, "
                "created_at FROM historical_summaries ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def search_history(self, query: str, limit: int = 3) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("history query must be non-empty text")
        limit = max(1, min(int(limit), 20))
        tokens = []
        for token in re.findall(r"[^\W_]+", query.casefold(), re.UNICODE):
            if len(token) >= 2 and token not in tokens:
                tokens.append(token)
        if not tokens:
            return []
        match_query = " OR ".join(
            '"' + token.replace('"', '""') + '"' for token in tokens
        )
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT h.id, h.from_message_id, h.through_message_id, "
                "h.summary, h.created_at "
                "FROM historical_summaries_fts "
                "JOIN historical_summaries AS h "
                "ON h.id = CAST(historical_summaries_fts.archive_id AS INTEGER) "
                "WHERE historical_summaries_fts MATCH ? "
                "ORDER BY bm25(historical_summaries_fts), h.id DESC LIMIT ?",
                (match_query, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _delete_messages_through(connection, message_id: int) -> int:
        """Delete raw messages already represented by consolidated state."""
        if message_id <= 0:
            return 0
        cursor = connection.execute(
            "DELETE FROM messages WHERE id <= ?",
            (message_id,),
        )
        return cursor.rowcount

    def prune_compacted_messages(self) -> int:
        """Remove legacy retained messages already covered by the summary."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT through_message_id FROM context_state WHERE id = 1"
            ).fetchone()
            return self._delete_messages_through(
                connection, row["through_message_id"]
            )

    def reset_context(self) -> None:
        """Discard conversational state while preserving long-term memory."""
        with self._connect() as connection:
            latest = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS id FROM messages"
            ).fetchone()["id"]
            connection.execute(
                "UPDATE context_state SET summary = '', through_message_id = ? "
                "WHERE id = 1",
                (latest,),
            )
            self._delete_messages_through(connection, latest)

    def memory_document(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM memory_state WHERE id = 1"
            ).fetchone()
        return row["document"]

    def set_memory_document(self, document: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE memory_state SET document = ? WHERE id = 1",
                (document.strip(),),
            )

    @staticmethod
    def _normalized_memory_line(line: str) -> str:
        line = line.strip()
        if line.startswith(("- ", "* ")):
            line = line[2:].strip()
        return " ".join(line.split()).casefold()

    def append_memory(self, content: str) -> bool:
        content = " ".join(content.split())
        if not content:
            raise ValueError("memory content must not be empty")
        normalized = self._normalized_memory_line(content)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM memory_state WHERE id = 1"
            ).fetchone()
            document = row["document"].strip()
            if any(
                self._normalized_memory_line(line) == normalized
                for line in document.splitlines()
            ):
                return False
            updated = "\n".join(filter(None, [document, f"- {content}"]))
            connection.execute(
                "UPDATE memory_state SET document = ? WHERE id = 1",
                (updated,),
            )
        return True

    def forget_memory(self, content: str, prefix: bool = False) -> bool:
        target = self._normalized_memory_line(content)
        if not target:
            raise ValueError("memory content must not be empty")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM memory_state WHERE id = 1"
            ).fetchone()
            lines = row["document"].splitlines()
            kept = []
            removed = False
            for line in lines:
                normalized = self._normalized_memory_line(line)
                matches = normalized == target or (
                    prefix and normalized.startswith(target + ":")
                )
                if matches:
                    removed = True
                else:
                    kept.append(line)
            if removed:
                connection.execute(
                    "UPDATE memory_state SET document = ? WHERE id = 1",
                    ("\n".join(kept).strip(),),
                )
        return removed
