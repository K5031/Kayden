"""SQLite Memory: append-only archive and rolling summary in SQLite; long-term memory as a
folder of Markdown files per project (memory.folder), as Claude Code keeps it.

The archive is the single source of truth for messages. Active context is the archived
messages after the latest summary's `through_seq`, so compaction removes nothing: it
inserts one summary row in a single transaction. All token figures come from the
runtime's `TokenCounter`; thresholds are fractions of the window.

The only removal from the archive is erasure the user asks for (delete_conversation). It runs with
`secure_delete` on, so erased content is overwritten in the file, and checkpoints the
write-ahead log afterwards so no copy stays there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Collection, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from functools import partial
from itertools import accumulate
from os import PathLike
from pathlib import Path
from typing import Any

from kayden.api import (
    MEMORY_INDEX,
    ArchiveHit,
    AssembledContext,
    Block,
    Completion,
    ContextRequest,
    ConversationId,
    ConversationInfo,
    CoreError,
    Duty,
    DutyKind,
    InstancePaths,
    Memory,
    MemoryFile,
    MemoryView,
    Message,
    OperationResult,
    PlanItem,
    PlanStatus,
    Reasoning,
    Role,
    ServerToolUse,
    Text,
    TokenCounter,
    ToolCall,
    ToolContext,
    ToolOutput,
    ToolResult,
    ToolSpec,
)
from kayden.memory.folder import RULES, MemoryFolder, project_key
from kayden.memory.tools import memory_tools, run_memory_tool

from . import codec

logger = logging.getLogger(__name__)
# A store of another version is refused.
SCHEMA_VERSION = 9
# Output headroom on top of the summary budget, for its tags.
_SUMMARY_TAG_TOKENS = 64  # the summary and topics tags
_TITLE_OUTPUT_TOKENS = 20
_TITLE_INPUT_CHARS = 2000
_TITLE_MAX_CHARS = 80
_PREVIEW_CHARS = 100
# Tool input and output beyond this is cut from what the summary model reads (start and end
# kept); the archive keeps it whole for recall.
_SUMMARY_TOOL_CHARS = 2_000


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    """Every value is a fraction of the context window (`ContextRequest.max_tokens`)."""

    trigger_fraction: float = 0.9  # compact once summary + active verbatim context exceed this
    remove_fraction: float = 0.5  # share of active tokens moved into the summary; 0.5 = cut-half
    summary_fraction: float = 0.05  # rolling-summary budget
    long_term_fraction: float = 0.05  # budget for the memory rules and MEMORY.md

    def __post_init__(self) -> None:
        for name in ("trigger_fraction", "remove_fraction", "summary_fraction",
                     "long_term_fraction"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name} must be in (0, 1]")


CUT_HALF = CompactionConfig(remove_fraction=0.5)


def factory(options: dict[str, Any], paths: InstancePaths) -> SqliteMemory:
    """Options: `path` (default `<data_dir>/memory.db`), `projects`, the folder of the
    projects' memory folders (default `<data_dir>/projects`), plus any CompactionConfig
    field."""
    options = dict(options)
    path = Path(options.pop("path", paths.data_dir / "memory.db"))
    projects = Path(options.pop("projects", paths.data_dir / "projects"))
    return SqliteMemory(path, CompactionConfig(**options), projects=projects)


factory.display_name = "Built-in memory (SQLite)"  # type: ignore[attr-defined]


# Messages are deleted only for a conversation listed in `erasing`, inside the
# transaction that erases it.
_NO_DELETE_TRIGGER = """
CREATE TABLE erasing (conversation_id TEXT PRIMARY KEY);
CREATE TRIGGER messages_no_delete BEFORE DELETE ON messages
WHEN OLD.conversation_id NOT IN (SELECT conversation_id FROM erasing)
BEGIN SELECT RAISE(ABORT, 'archive is append-only'); END;
"""
_FTS_TABLE = """CREATE VIRTUAL TABLE messages_fts USING fts5(
    body, content='', tokenize='porter unicode61 remove_diacritics 2'
);"""
_SCHEMA = f"""
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    seed_from TEXT,
    folder TEXT,
    duty TEXT,
    plan TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    id TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at REAL NOT NULL,
    format INTEGER NOT NULL,
    blocks TEXT NOT NULL,
    archived_at REAL NOT NULL,
    UNIQUE (conversation_id, id)
);
CREATE INDEX messages_conversation ON messages(conversation_id, seq);
CREATE TRIGGER messages_no_update BEFORE UPDATE ON messages
BEGIN SELECT RAISE(ABORT, 'archive is append-only'); END;
{_NO_DELETE_TRIGGER}
{_FTS_TABLE}
CREATE TABLE summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    through_seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX summaries_conversation ON summaries(conversation_id, id);
"""
_TRUNCATION_MARKER = (
    "\n[{omitted} characters truncated in this view. The full message ({id}) is kept in the"
    " archive; use recall with words from it to read the rest.]"
)

_SYSTEM_PROMPT = """\
You maintain the rolling summary of a conversation between a user and an AI agent.
Merge the previous summary with the transcript excerpt that follows into an updated
summary of at most {summary_tokens} tokens. Keep what is needed to continue the work:
goals, decisions, facts, open tasks and their status. Drop details that no longer
matter; the original messages stay searchable in an archive.

Then list the topics covered so far, one short line each with its date: every topic
from the previous summary's list, then the new ones. Never drop a topic; when the list
grows long, merge closely related ones into one line. The list tells the agent what it
can search the archive for.

Reply exactly in this format:
<summary>
updated summary
</summary>
<topics>
- topic (date)
</topics>

Previous summary:
{summary}
"""

_TITLE_PROMPT = """\
Write a title of at most six words for a conversation that starts with the user's message
below. Reply with the title only: no quotes, no punctuation at the end."""

_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
_TOPICS_RE = re.compile(r"<topics>(.*?)</topics>", re.DOTALL)
TOPICS_HEADING = "Topics so far (search the history for details):"
_WORD_RE = re.compile(r"\w+")
_INFO_COLUMNS = "id, title, created_at, updated_at, archived, seed_from, folder, duty, plan"
_NEW_COLUMNS = "id, title, created_at, updated_at, archived, seed_from, folder"
@dataclass(frozen=True, slots=True)
class _Active:
    seq: int
    message: Message


@dataclass(frozen=True, slots=True)
class _State:
    info: ConversationInfo
    summary: str
    active: list[_Active]
    seeded: bool  # a summary row exists
    # The message the current turn started from, when compaction cut inside that turn: it
    # stays in view before the active messages, so the agent keeps the task's wording.
    task: Message | None = None

    def view(self) -> list[Message]:
        return ([self.task] if self.task else []) + [a.message for a in self.active]


class SqliteMemory(Memory):
    """Memory on one SQLite file (WAL, so another process may read it concurrently). All
    database work runs on a single worker thread."""

    def __init__(
        self, path: str | PathLike[str], config: CompactionConfig = CUT_HALF, *,
        projects: str | PathLike[str] | None = None,
    ) -> None:
        self._path = Path(path)
        # Long-term memory: a folder of files per project (memory.folder).
        self._projects = Path(projects) if projects is not None else self._path.parent / "projects"
        self._config = config
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kayden-memory")
        self._conn: sqlite3.Connection | None = None
        self._locks: defaultdict[ConversationId, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ── Conversations ─────────────────────────────────────────────────────

    async def create_conversation(
        self,
        *,
        title: str | None = None,
        seed_from: ConversationId | None = None,
        folder: str | None = None,
        conversation_id: ConversationId | None = None,
    ) -> ConversationInfo:
        now = time.time()
        info = ConversationInfo(conversation_id or uuid.uuid4().hex, title or None, now, now,
                                seed_from=seed_from, folder=folder)

        def run(conn: sqlite3.Connection) -> None:
            with _transaction(conn):
                if seed_from is not None:
                    _require_conversation(conn, seed_from)
                if conn.execute("SELECT 1 FROM conversations WHERE id = ?",
                                (info.id,)).fetchone():
                    raise ValueError(f"conversation {info.id} exists")
                conn.execute(
                    f"INSERT INTO conversations ({_NEW_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (info.id, info.title, now, now, False, seed_from, folder),
                )

        await self._run(run)
        return info

    async def list_conversations(
        self, *, include_archived: bool = False
    ) -> Sequence[ConversationInfo]:
        def run(conn: sqlite3.Connection) -> list[ConversationInfo]:
            rows = conn.execute(
                f"SELECT {_INFO_COLUMNS}, (SELECT blocks FROM messages m"
                " WHERE m.conversation_id = c.id AND m.role = 'user' ORDER BY seq LIMIT 1)"
                " FROM conversations c WHERE ? OR archived = 0"
                " ORDER BY updated_at DESC, created_at DESC",
                (include_archived,),
            )
            return [_conversation_info(row[:9], row[9]) for row in rows]

        return await self._run(run)

    async def update_conversation(
        self,
        conversation_id: ConversationId,
        *,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationInfo:
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = title.strip() or None
        if archived is not None:
            changes["archived"] = archived

        def run(conn: sqlite3.Connection) -> ConversationInfo:
            with _transaction(conn):
                _require_conversation(conn, conversation_id)
                if changes:
                    conn.execute(
                        f"UPDATE conversations SET {', '.join(f'{k} = ?' for k in changes)}"
                        " WHERE id = ?",
                        (*changes.values(), conversation_id),
                    )
                return _require_conversation(conn, conversation_id)

        return await self._run(run)

    async def save_duty(self, conversation_id: ConversationId, duty: Duty | None,
                        plan: Sequence[PlanItem]) -> ConversationInfo:
        encoded = None if duty is None else json.dumps(asdict(duty))
        items = json.dumps([asdict(i) for i in plan], ensure_ascii=False)

        def run(conn: sqlite3.Connection) -> ConversationInfo:
            with _transaction(conn):
                _require_conversation(conn, conversation_id)
                conn.execute("UPDATE conversations SET duty = ?, plan = ? WHERE id = ?",
                             (encoded, items, conversation_id))
                return _require_conversation(conn, conversation_id)

        return await self._run(run)

    async def delete_conversation(self, conversation_id: ConversationId) -> None:
        def run(conn: sqlite3.Connection) -> None:
            with _transaction(conn):
                _require_conversation(conn, conversation_id)
                conn.execute("INSERT INTO erasing VALUES (?)", (conversation_id,))
                rows = conn.execute(
                    "SELECT seq, blocks FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchall()
                # A contentless FTS5 index forgets a row only when given its original text.
                conn.executemany(
                    "INSERT INTO messages_fts (messages_fts, rowid, body) VALUES ('delete', ?, ?)",
                    [(seq, codec.searchable_text(blocks)) for seq, blocks in rows],
                )
                conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
                conn.execute("DELETE FROM erasing WHERE conversation_id = ?", (conversation_id,))
                conn.execute("DELETE FROM summaries WHERE conversation_id = ?",
                             (conversation_id,))
                conn.execute("UPDATE conversations SET seed_from = NULL WHERE seed_from = ?",
                             (conversation_id,))
                conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
                # Rewrite the index without the deleted rows; until then their terms stay.
                conn.execute("INSERT INTO messages_fts (messages_fts) VALUES ('optimize')")
            _checkpoint(conn)

        async with self._locks[conversation_id]:
            await self._run(run)
        self._locks.pop(conversation_id, None)

    # ── Transcript ────────────────────────────────────────────────────────

    async def append(
        self, conversation_id: ConversationId, messages: Sequence[Message]
    ) -> None:
        rows = [
            (m, m.role.value, m.created_at, codec.encode_blocks(m.blocks)) for m in messages
        ]

        def run(conn: sqlite3.Connection) -> None:
            now = time.time()
            with _transaction(conn):
                _require_conversation(conn, conversation_id)
                for message, role, created_at, blocks in rows:
                    stored = conn.execute(
                        "SELECT id, role, created_at, blocks FROM messages"
                        " WHERE conversation_id = ? AND id = ?",
                        (conversation_id, message.id),
                    ).fetchone()
                    if stored is not None:
                        if _message(stored) != message:
                            raise ValueError(
                                f"message {message.id} was already appended with other content"
                            )
                        continue
                    cursor = conn.execute(
                        "INSERT INTO messages (conversation_id, id, role, created_at, format,"
                        " blocks, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (conversation_id, message.id, role, created_at,
                         codec.FORMAT_VERSION, blocks, now),
                    )
                    conn.execute(
                        "INSERT INTO messages_fts (rowid, body) VALUES (?, ?)",
                        (cursor.lastrowid, codec.searchable_text(blocks)),
                    )
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )

        async with self._locks[conversation_id]:
            await self._run(run)

    async def history(
        self,
        conversation_id: ConversationId,
        *,
        before: str | None = None,
        limit: int | None = None,
        after: str | None = None,
    ) -> Sequence[Message]:
        def seq_of(conn: sqlite3.Connection, message_id: str | None) -> int | None:
            if message_id is None:
                return None
            row = conn.execute(
                "SELECT seq FROM messages WHERE conversation_id = ? AND id = ?",
                (conversation_id, message_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown message {message_id} in {conversation_id}")
            return int(row[0])

        def run(conn: sqlite3.Connection) -> list[Message]:
            with _transaction(conn, "DEFERRED"):
                _require_conversation(conn, conversation_id)
                upper, lower = seq_of(conn, before), seq_of(conn, after)
                rows = conn.execute(
                    "SELECT id, role, created_at, blocks FROM messages"
                    " WHERE conversation_id = ? AND (? IS NULL OR seq < ?)"
                    " AND (? IS NULL OR seq > ?)"
                    f" ORDER BY seq {'ASC' if after is not None else 'DESC'} LIMIT ?",
                    (conversation_id, upper, upper, lower, lower,
                     -1 if limit is None else limit),
                ).fetchall()
            return [_message(row) for row in (rows if after is not None else reversed(rows))]

        return await self._run(run)

    async def assemble(self, request: ContextRequest) -> AssembledContext:
        state = await self._run(_read_state, request.conversation_id)
        index = await asyncio.to_thread(self._folder(state.info.folder).index)
        return _fit(state, index, request, self._config.long_term_fraction)

    # ── Maintenance ───────────────────────────────────────────────────────

    async def maintain(
        self, conversation_id: ConversationId, complete: Completion, context: ContextRequest
    ) -> bool:
        async with self._locks[conversation_id]:
            state = await self._run(_read_state, conversation_id)
            acted = False
            seeding = state.info.seed_from is not None and not state.seeded
            if seeding:
                seeded = await self._seed(state, complete, context)
                if seeded:
                    state = await self._run(_read_state, conversation_id)
                acted = bool(seeded)
            # A first summary from compaction would end seeding: wait for the seed.
            if not (seeding and seeded is None):
                acted = await self._compact(state, complete, context) or acted
            if state.info.title is None:
                acted = await self._title(conversation_id, complete) or acted
            return acted

    def _prompt(self, state: _State, summary: str, context: ContextRequest) -> tuple[str, int]:
        summary_tokens = max(1, int(self._config.summary_fraction * context.max_tokens))
        system = _SYSTEM_PROMPT.format(summary_tokens=summary_tokens,
                                       summary=summary or "(none)")
        return system, summary_tokens

    async def _seed(self, state: _State, complete: Completion, context: ContextRequest
                    ) -> bool | None:
        """Write the first summary of a conversation continued from another: the source's
        summary merged with as much of its verbatim tail as the light model takes. False
        when the source is empty; None when the summary came back empty (tried again by the
        next maintain)."""
        count = context.count_tokens
        source = await self._run(_read_state, state.info.seed_from)
        messages = source.view()
        if not source.summary and not messages:
            return False
        system, summary_tokens = self._prompt(state, source.summary, context)
        output_tokens = summary_tokens + _SUMMARY_TAG_TOKENS
        allowance = complete.max_input_tokens(light=True) - output_tokens - count(system)
        starts = _valid_starts(messages, pending_calls_block=False) if messages else []
        first = _max_true(len(starts), lambda k: k == 0 or count(
            [_user_message(_render_transcript(messages[starts[-k]:]))]) <= allowance)
        transcript = _render_transcript(messages[starts[-first]:]) if first else "(none)"
        output = await complete(
            system, [_user_message(transcript)], max_tokens=output_tokens, light=True
        )
        summary = _parse_output(output, summary_tokens, count)
        if not summary:
            return None

        def commit(conn: sqlite3.Connection) -> None:
            with _transaction(conn):
                _replace_summary(conn, state.info.id, 0, summary, time.time())

        await self._run(commit)
        return True

    async def _compact(
        self, state: _State, complete: Completion, context: ContextRequest
    ) -> bool:
        config, count = self._config, context.count_tokens
        conversation_id = state.info.id
        sizes = [count([a.message]) for a in state.active]
        total = (sum(sizes) + (count(state.summary) if state.summary else 0)
                 + (count([state.task]) if state.task else 0))
        if total <= config.trigger_fraction * context.max_tokens:
            return False
        system, summary_tokens = self._prompt(state, state.summary, context)
        output_tokens = summary_tokens + _SUMMARY_TAG_TOKENS
        allowance = complete.max_input_tokens(light=True) - output_tokens - count(system)
        selection = _choose_cut(state.active, sizes, config.remove_fraction, allowance, count)
        if selection is None:
            return False
        cut, transcript = selection
        output = await complete(
            system, [_user_message(transcript)], max_tokens=output_tokens, light=True,
            compacting=True,
        )
        summary = _parse_output(output, summary_tokens, count)
        if not summary:  # e.g. reasoning used the whole output limit: keep the context
            logger.warning("compaction of %s returned no summary; nothing compacted",
                           conversation_id)
            return False
        through_seq = state.active[cut - 1].seq

        def commit(conn: sqlite3.Connection) -> None:
            with _transaction(conn):
                _replace_summary(conn, conversation_id, through_seq, summary, time.time())

        await self._run(commit)
        return True

    async def _title(self, conversation_id: ConversationId, complete: Completion) -> bool:
        def first_text(conn: sqlite3.Connection) -> str:
            for (blocks,) in conn.execute(
                "SELECT blocks FROM messages WHERE conversation_id = ? AND role = 'user'"
                " ORDER BY seq", (conversation_id,)
            ):
                if text := _text_of(blocks):
                    return text
            return ""

        text = await self._run(first_text)
        if not text:
            return False
        try:
            output = await complete(
                _TITLE_PROMPT, [_user_message(text[:_TITLE_INPUT_CHARS])],
                max_tokens=_TITLE_OUTPUT_TOKENS, light=True,
            )
        except CoreError as e:  # leave it untitled; the next maintain tries again
            logger.warning("title for %s failed: %s", conversation_id, e)
            return False
        title = _clean_title(output)
        if not title:
            return False

        def commit(conn: sqlite3.Connection) -> bool:
            with _transaction(conn):  # a title the user set meanwhile wins
                return conn.execute(
                    "UPDATE conversations SET title = ? WHERE id = ? AND title IS NULL",
                    (title, conversation_id),
                ).rowcount == 1

        return await self._run(commit)

    # ── Search and inspection ─────────────────────────────────────────────

    async def search(
        self,
        query: str,
        *,
        conversation_ids: Collection[ConversationId] | None = None,
        limit: int = 5,
    ) -> Sequence[ArchiveHit]:
        words = _WORD_RE.findall(query)
        match = " OR ".join(f'"{w}"' for w in dict.fromkeys(words))
        scope = None if conversation_ids is None else list(dict.fromkeys(conversation_ids))
        sql = (
            "SELECT m.conversation_id, m.id, m.role, m.created_at, m.blocks,"
            " bm25(messages_fts) AS rank"
            " FROM messages_fts JOIN messages m ON m.seq = messages_fts.rowid"
            " WHERE messages_fts MATCH ?"
        )
        params: list[Any] = [match]
        if scope is not None:
            sql += f" AND m.conversation_id IN ({', '.join('?' * len(scope))})"
            params.extend(scope)
        sql += " ORDER BY rank, m.seq LIMIT ?"
        params.append(limit)

        def run(conn: sqlite3.Connection) -> list[ArchiveHit]:
            with _transaction(conn, "DEFERRED"):
                for conversation_id in scope or ():
                    _require_conversation(conn, conversation_id)
                if not words or limit <= 0 or scope == []:
                    return []
                return [
                    ArchiveHit(row[0], _message(row[1:5]), -row[5])
                    for row in conn.execute(sql, params)
                ]

        return await self._run(run)

    async def inspect(self, conversation_id: ConversationId) -> MemoryView:
        state = await self._run(_read_state, conversation_id, False)
        index = await asyncio.to_thread(self._folder(state.info.folder).index)
        return MemoryView(summary=state.summary, long_term=[index] if index else [])

    # ── Tools ─────────────────────────────────────────────────────────────

    async def list_tools(self) -> Sequence[ToolSpec]:
        return memory_tools(editable=True)

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutput:
        return await run_memory_tool(self, call, ctx)

    # ── Long-term memory ──────────────────────────────────────────────────

    def _folder(self, folder: str | None) -> MemoryFolder:
        """The memory folder of a project: `<projects>/<key>/memory`."""
        return MemoryFolder(self._projects / project_key(folder) / "memory")

    async def memory_files(self, folder: str | None) -> Sequence[MemoryFile]:
        return await asyncio.to_thread(self._folder(folder).files)

    async def write_memory_file(self, folder: str | None, path: str, text: str) -> MemoryFile:
        return await asyncio.to_thread(self._folder(folder).write, path, text)

    async def delete_memory_file(self, folder: str | None, path: str) -> None:
        await asyncio.to_thread(self._folder(folder).delete, path)

    async def aclose(self) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.close()
            self._conn = None

        if self._conn is not None:
            await self._run(run)
        self._executor.shutdown(wait=False)

    # ── Internals ─────────────────────────────────────────────────────────

    async def _run[T](self, fn: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(self._call, fn, *args))

    def _call[T](self, fn: Callable[..., T], *args: Any) -> T:
        if self._conn is None:
            self._conn = _open(self._path)
        return fn(self._conn, *args)


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA secure_delete = ON")  # erased content is overwritten with zeros
        with _transaction(conn):
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                _execute_script(conn, _SCHEMA)
            elif version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"{path}: memory schema version {version} is not supported"
                    f" (expected {SCHEMA_VERSION})"
                )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except BaseException:
        conn.close()
        raise
    return conn


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """executescript() would commit the open transaction; run statements singly."""
    for statement in script.split(";\n"):
        if statement.strip():
            conn.execute(statement)


def _checkpoint(conn: sqlite3.Connection) -> None:
    """Move everything into the database file and empty the write-ahead log, so erased
    content leaves no copy there. Another process reading at the time can keep it from
    completing; the next erasure or SQLite's own checkpoints finish the job."""
    busy, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy:
        logger.warning("write-ahead log not emptied: another connection is reading")


@contextmanager
def _transaction(conn: sqlite3.Connection, kind: str = "IMMEDIATE") -> Iterator[None]:
    conn.execute(f"BEGIN {kind}")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _require_conversation(
    conn: sqlite3.Connection, conversation_id: ConversationId
) -> ConversationInfo:
    row = conn.execute(
        f"SELECT {_INFO_COLUMNS} FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown conversation {conversation_id}")
    return _conversation_info(row)


def _conversation_info(row: Sequence[Any], first_user_blocks: str | None = None
                       ) -> ConversationInfo:
    preview = " ".join(_text_of(first_user_blocks).split()) if first_user_blocks else ""
    duty = None
    if row[7] is not None:
        d = json.loads(row[7])
        duty = Duty(d["instruction"], DutyKind(d["kind"]), d["min_interval_s"],
                    d["max_interval_s"])
    plan = tuple(PlanItem(i["text"], PlanStatus(i["status"]), i["due"], i["note"])
                 for i in json.loads(row[8]))
    return ConversationInfo(row[0], row[1], row[2], row[3],
                            archived=bool(row[4]), seed_from=row[5],
                            preview=preview[:_PREVIEW_CHARS] or None, folder=row[6],
                            duty=duty, plan=plan)


def _text_of(blocks: str) -> str:
    return "\n".join(b.text for b in codec.decode_blocks(blocks) if isinstance(b, Text)).strip()


def _clean_title(output: str) -> str:
    line = next((ln for ln in output.splitlines() if ln.strip()), "")
    title = line.strip().strip("#*_`\"'“”‘’ ").removeprefix("Title:").strip().rstrip(".")
    return title[:_TITLE_MAX_CHARS].strip()


def _replace_summary(conn: sqlite3.Connection, conversation_id: ConversationId,
                     through_seq: int, text: str, now: float) -> None:
    """A conversation keeps only its newest summary: older ones are derived from messages
    the archive still holds, and nothing reads them."""
    conn.execute("DELETE FROM summaries WHERE conversation_id = ?", (conversation_id,))
    conn.execute(
        "INSERT INTO summaries (conversation_id, through_seq, text, created_at)"
        " VALUES (?, ?, ?, ?)",
        (conversation_id, through_seq, text, now),
    )


def _message(row: Sequence[Any]) -> Message:
    return Message(row[0], Role(row[1]), codec.decode_blocks(row[3]), row[2])


def _user_message(text: str) -> Message:
    return Message(f"compaction-{uuid.uuid4().hex}", Role.USER, (Text(text),), time.time())


def _read_state(
    conn: sqlite3.Connection, conversation_id: ConversationId, with_active: bool = True
) -> _State:
    with _transaction(conn, "DEFERRED"):
        info = _require_conversation(conn, conversation_id)
        row = conn.execute(
            "SELECT through_seq, text FROM summaries WHERE conversation_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        through_seq, summary = row if row else (0, "")
        rows = (
            conn.execute(
                "SELECT seq, id, role, created_at, blocks FROM messages"
                " WHERE conversation_id = ? AND seq > ? ORDER BY seq",
                (conversation_id, through_seq),
            ).fetchall()
            if with_active
            else []
        )
        task = None
        if rows and rows[0][2] != Role.USER.value:
            task_row = conn.execute(
                "SELECT id, role, created_at, blocks FROM messages WHERE conversation_id = ?"
                " AND seq <= ? AND role = ? ORDER BY seq DESC LIMIT 1",
                (conversation_id, through_seq, Role.USER.value),
            ).fetchone()
            task = _message(task_row) if task_row else None
    active = [_Active(r[0], _message(r[1:])) for r in rows]
    return _State(info, summary, active, row is not None, task)


def _valid_starts(messages: Sequence[Message], *, pending_calls_block: bool) -> list[int]:
    """Indices i of USER messages where messages[i:] orphans no tool result.

    Falls back to any role when no USER message qualifies (a transcript the runtime would
    never write).
    """
    starts = _open_starts(messages, pending_calls_block=pending_calls_block)
    return [i for i in starts if messages[i].role is Role.USER] or starts


def _open_starts(messages: Sequence[Message], *, pending_calls_block: bool) -> list[int]:
    """Indices i where messages[i:] orphans no tool result.

    With pending_calls_block, a call that has no result yet also pins the cut before it,
    so its later result never lands after its compacted call.
    """
    n = len(messages)
    blocked = [0] * (n + 2)
    calls: dict[str, int] = {}
    for i, message in enumerate(messages):
        for block in message.blocks:
            if isinstance(block, ToolCall):
                calls[block.id] = i
            elif isinstance(block, ToolResult) and block.call_id in calls:
                blocked[calls.pop(block.call_id) + 1] += 1
                blocked[i + 1] -= 1
    if pending_calls_block:
        for i in calls.values():
            blocked[i + 1] += 1
            blocked[n + 1] -= 1
    starts, depth = [], 0
    for i in range(n):
        depth += blocked[i]
        if depth == 0:
            starts.append(i)
    return starts


def _choose_cut(
    active: Sequence[_Active], sizes: Sequence[int], remove_fraction: float, allowance: int,
    count: TokenCounter,
) -> tuple[int, str] | None:
    """The compaction cut and the transcript to summarize, whose message fits `allowance`."""
    messages = [a.message for a in active]
    starts = _open_starts(messages, pending_calls_block=True)
    candidates = [s for s in starts if s >= 1 and messages[s].role is Role.USER]
    target = sum(sizes) * remove_fraction
    cumulative = list(accumulate(sizes, initial=0))
    if not candidates or cumulative[candidates[-1]] < target:  # a long turn: cut inside it
        candidates = [s for s in starts
                      if s >= 1 and messages[s].role in (Role.USER, Role.ASSISTANT)]
    if not candidates or allowance <= 0:
        return None
    wanted = next((s for s in candidates if cumulative[s] >= target), candidates[-1])
    candidates = [s for s in candidates if s <= wanted]

    def transcript(cut: int) -> str:
        return _render_transcript(messages[:cut])

    def fits(text: str) -> bool:
        return count([_user_message(text)]) <= allowance

    fitting = _max_true(len(candidates), lambda k: k == 0 or
                        fits(transcript(candidates[k - 1])))
    if fitting:
        cut = candidates[fitting - 1]
        return cut, transcript(cut)
    cut, text = candidates[0], transcript(candidates[0])  # one oversized exchange
    keep = _max_true(len(text), lambda n: fits(text[:n]))
    return (cut, text[:keep]) if keep else None


def _render_transcript(messages: Sequence[Message]) -> str:
    lines = []
    for message in messages:
        stamp = datetime.fromtimestamp(message.created_at, UTC).strftime("%Y-%m-%d %H:%M")
        for block in message.blocks:
            match block:
                case Text(text):
                    lines.append(f"[{stamp} {message.role}] {text}")
                case Reasoning():
                    continue
                case ToolCall(_, name, arguments, raw):
                    shown = _shortened(raw or str(arguments))
                    lines.append(f"[{stamp} {message.role} calls {name}] {shown}")
                case ToolResult(_, status, content):
                    lines.append(f"[{stamp} tool result, {status}] {_shortened(content)}")
                case OperationResult(call_id, status, content):
                    lines.append(f"[{stamp} background operation {call_id}, {status}] "
                                 f"{_shortened(content)}")
                case ServerToolUse(summary=summary, content=content):
                    lines.append(f"[{stamp} {message.role}] {summary}: {_shortened(content)}")
                case _:
                    lines.append(f"[{stamp} {message.role}] {block}")
    return "\n".join(lines)


def _shortened(text: str) -> str:
    """Tool input or output as the summary model reads it: its start and end."""
    if len(text) <= _SUMMARY_TOOL_CHARS:
        return text
    half = _SUMMARY_TOOL_CHARS // 2
    return (f"{text[:half]} [... {len(text) - 2 * half} characters left out; recall finds "
            f"them ...] {text[-half:]}")


def _parse_output(output: str, summary_tokens: int, count: TokenCounter) -> str:
    """The summary, then the topics list under TOPICS_HEADING. Over budget, the prose is
    cut first: the topics get up to half the budget, whole lines from the top."""
    summary_match, topics_match = _SUMMARY_RE.search(output), _TOPICS_RE.search(output)
    summary = (summary_match.group(1) if summary_match else
               _TOPICS_RE.sub("", output) if topics_match else output).strip()
    lines = [line.strip() for line in (topics_match.group(1) if topics_match else "")
             .splitlines() if line.strip()]
    if not summary:
        return ""
    keep = _max_true(len(lines), lambda k: count(_topics(lines[:k])) <= summary_tokens // 2)
    topics = _topics(lines[:keep])
    room = summary_tokens - (count(topics) if topics else 0)
    if count(summary) > room:
        cut = summary[: _max_true(len(summary), lambda n: count(summary[:n]) <= room)]
        summary = cut.rsplit(None, 1)[0] if " " in cut.strip() else cut
    return f"{summary}\n\n{topics}" if topics else summary


def _topics(lines: Sequence[str]) -> str:
    return "\n".join([TOPICS_HEADING, *lines]) if lines else ""


def _render_addendum(summary: str, memory: bool, index: Sequence[str]) -> str:
    """The conversation summary, then with `memory` the memory rules and the lines of the
    project's MEMORY_INDEX."""
    parts = []
    if summary:
        parts.append(f"## Conversation summary\n{summary}")
    if memory:
        parts.append(f"{RULES}\n\n## {MEMORY_INDEX}\n"
                     + ("\n".join(index) if index else "(empty: nothing saved yet)"))
    return "\n\n".join(parts)


def _fit(state: _State, memory_index: str, request: ContextRequest, long_term_fraction: float
         ) -> AssembledContext:
    """The newest exchange (from the last USER message on) is always included, truncated in
    view if it alone exceeds the budget. The addendum (summary, then the memory rules and
    as much of the project's MEMORY_INDEX, from the top, as fits the long-term budget) gets
    what remains, and the verbatim tail extends back as far as still fits."""
    count, budget = request.count_tokens, request.max_tokens
    messages = state.view()
    starts = _valid_starts(messages, pending_calls_block=False)
    newest = list(messages[starts[-1]:]) if starts else []
    view = _truncate_to_fit(newest, budget, count)
    room = budget - (count(view) if view else 0)

    def size(addendum: str) -> int:
        return count(addendum) if addendum else 0

    lines = [line for line in memory_index.splitlines() if line.strip()]
    rules = size(_render_addendum("", True, []))
    memory = rules <= room  # the rules come whenever they fit; the index within its budget
    keep = _max_true(len(lines), lambda k: size(_render_addendum("", True, lines[:k])) - rules
                     <= long_term_fraction * budget) if memory else 0
    keep = _max_true(keep, lambda k: size(
        _render_addendum(state.summary, memory, lines[:k])) <= room)
    index = lines[:keep]
    summary = state.summary
    if size(_render_addendum(summary, memory, index)) > room:
        summary = summary[: _max_true(len(summary), lambda n: size(
            _render_addendum(summary[:n], memory, index)) <= room)]
    addendum = _render_addendum(summary, memory, index)
    if view != newest:
        return AssembledContext(addendum, view)

    room = budget - size(addendum)
    first = _max_true(len(starts) - 1, lambda k: count(messages[starts[-1 - k]:]) <= room)
    return AssembledContext(addendum, messages[starts[-1 - first]:] if starts else [])


def _truncate_to_fit(
    messages: list[Message], budget: int, count: TokenCounter
) -> list[Message]:
    """Shrink the largest messages until the list fits, or nothing more can be cut."""
    view = list(messages)
    shrinkable = set(range(len(view)))
    while shrinkable and count(view) > budget:
        j = max(shrinkable, key=lambda k: count([view[k]]))
        target = count([view[j]]) - (count(view) - budget)
        view[j] = _shrink(messages[j], target, count)
        shrinkable.discard(j)
    return view


def _shrink(message: Message, target: int, count: TokenCounter) -> Message:
    """Cut every text-bearing block to the longest common length that keeps `message`
    within `target` tokens. Reasoning is dropped: it cannot be cut and still verify."""
    longest = max((len(_body(b)) for b in message.blocks if _body(b) is not None), default=0)

    def cut(n: int) -> Message:
        blocks: list[Block] = []
        for block in message.blocks:
            body = _body(block)
            if isinstance(block, Reasoning):
                continue
            if body is None or len(body) <= n:
                blocks.append(block)
                continue
            text = body[:n] + _TRUNCATION_MARKER.format(omitted=len(body) - n, id=message.id)
            blocks.append(Text(text) if isinstance(block, Text) else replace(block, content=text))
        return replace(message, blocks=tuple(blocks))

    return cut(_max_true(longest, lambda n: count([cut(n)]) <= target))


def _body(block: Block) -> str | None:
    match block:
        case Text(text):
            return text
        case ToolResult(content=content) | OperationResult(content=content):
            return content
    return None


def _max_true(upper: int, predicate: Callable[[int], bool]) -> int:
    """Largest n in [0, upper] with predicate(n), assuming it is monotone decreasing."""
    lo, hi = 0, upper
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if predicate(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo
