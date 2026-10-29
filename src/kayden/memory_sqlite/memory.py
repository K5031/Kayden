"""SQLite Memory: append-only archive, rolling summary, long-term entries.

The archive is the single source of truth for messages. Active context is the archived
messages after the latest summary's `through_seq`, so compaction removes nothing: it
inserts one summary row (plus any long-term entries) in a single transaction.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from os import PathLike
from pathlib import Path
from typing import Any

from kayden.api import (
    ArchiveHit,
    AssembledContext,
    Completion,
    ContextRequest,
    ConversationId,
    ConversationInfo,
    Memory,
    MemoryView,
    Message,
    Mode,
    Reasoning,
    Role,
    Text,
    ToolCall,
    ToolResult,
)

from . import codec

SCHEMA_VERSION = 1
CHARS_PER_TOKEN = 4
# Output headroom on top of the summary budget for tags and new long-term entries.
_LONG_TERM_OUTPUT_TOKENS = 512


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    """Token values are Memory's own estimates (CHARS_PER_TOKEN characters per token)."""

    trigger_tokens: int = 48_000  # compact once the active verbatim context reaches this
    remove_fraction: float = 0.5  # share of active tokens moved into the summary; 0.5 = cut-half
    summary_tokens: int = 2_000  # fixed rolling-summary budget
    long_term_tokens: int = 1_000  # budget for long-term entries rendered into the prompt

    def __post_init__(self) -> None:
        if not 0 < self.remove_fraction <= 1:
            raise ValueError("remove_fraction must be in (0, 1]")
        if min(self.trigger_tokens, self.summary_tokens, self.long_term_tokens) <= 0:
            raise ValueError("token budgets must be positive")


CUT_HALF = CompactionConfig(remove_fraction=0.5)

_SCHEMA = """
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    title TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
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
CREATE TRIGGER messages_no_delete BEFORE DELETE ON messages
BEGIN SELECT RAISE(ABORT, 'archive is append-only'); END;
CREATE VIRTUAL TABLE messages_fts USING fts5(
    body, content='', tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TABLE summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    through_seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX summaries_conversation ON summaries(conversation_id, id);
CREATE TABLE long_term (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    text TEXT NOT NULL,
    source_conversation_id TEXT NOT NULL REFERENCES conversations(id),
    created_at REAL NOT NULL
);
CREATE INDEX long_term_mode ON long_term(mode, id);
"""

_SYSTEM_PROMPT = """\
You maintain the rolling summary of a conversation between a user and an AI agent.
Merge the previous summary with the transcript excerpt that follows into an updated
summary of at most {summary_tokens} tokens. Keep what is needed to continue the work:
goals, decisions, facts, open tasks and their status. Drop details that no longer
matter; the original messages stay searchable in an archive.

Also list durable facts worth remembering beyond this conversation (user preferences,
stable facts about the user or environment) that are not already in long-term memory.
List nothing if there are none.

Reply exactly in this format:
<summary>
updated summary
</summary>
<long_term>
- one new fact per line
</long_term>

Previous summary:
{summary}

Existing long-term memory:
{long_term}
"""

_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
_LONG_TERM_RE = re.compile(r"<long_term>(.*?)</long_term>", re.DOTALL)
_WORD_RE = re.compile(r"\w+")


@dataclass(frozen=True, slots=True)
class _Active:
    seq: int
    message: Message
    tokens: int


@dataclass(frozen=True, slots=True)
class _State:
    mode: Mode
    summary: str
    long_term: list[str]
    active: list[_Active]


class SqliteMemory(Memory):
    """Memory on one SQLite file. All database work runs on a single worker thread."""

    def __init__(
        self, path: str | PathLike[str], config: CompactionConfig = CUT_HALF
    ) -> None:
        self._path = Path(path)
        self._config = config
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kayden-memory")
        self._conn: sqlite3.Connection | None = None
        self._locks: defaultdict[ConversationId, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ── Memory API ────────────────────────────────────────────────────────

    async def create_conversation(
        self, mode: Mode, title: str | None = None
    ) -> ConversationInfo:
        info = ConversationInfo(uuid.uuid4().hex, mode, title, time.time(), time.time())

        def run(conn: sqlite3.Connection) -> None:
            with _transaction(conn):
                conn.execute(
                    "INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
                    (info.id, info.mode.value, title, info.created_at, info.updated_at),
                )

        await self._run(run)
        return info

    async def list_conversations(self, mode: Mode) -> Sequence[ConversationInfo]:
        def run(conn: sqlite3.Connection) -> list[ConversationInfo]:
            rows = conn.execute(
                "SELECT id, mode, title, created_at, updated_at FROM conversations"
                " WHERE mode = ? ORDER BY updated_at DESC, created_at DESC",
                (mode.value,),
            )
            return [_conversation_info(row) for row in rows]

        return await self._run(run)

    async def append(
        self, conversation_id: ConversationId, messages: Sequence[Message]
    ) -> None:
        if not messages:
            return
        rows = [
            (m.id, m.role.value, m.created_at, codec.encode_blocks(m.blocks)) for m in messages
        ]

        def run(conn: sqlite3.Connection) -> None:
            now = time.time()
            with _transaction(conn):
                _require_conversation(conn, conversation_id)
                for message_id, role, created_at, blocks in rows:
                    cursor = conn.execute(
                        "INSERT INTO messages (conversation_id, id, role, created_at, format,"
                        " blocks, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (conversation_id, message_id, role, created_at,
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

    async def assemble(self, request: ContextRequest) -> AssembledContext:
        state = await self._run(_read_state, request.conversation_id)
        return _fit(state, request, self._config.long_term_tokens)

    async def maintain(self, conversation_id: ConversationId, complete: Completion) -> None:
        async with self._locks[conversation_id]:
            state = await self._run(_read_state, conversation_id)
            cut = _choose_cut(state.active, self._config)
            if cut is None:
                return
            selected = [a.message for a in state.active[:cut]]
            system = _SYSTEM_PROMPT.format(
                summary_tokens=self._config.summary_tokens,
                summary=state.summary or "(none)",
                long_term="\n".join(f"- {e}" for e in state.long_term) or "(none)",
            )
            transcript = Message(
                id=f"compaction-{uuid.uuid4().hex}",
                role=Role.USER,
                blocks=(Text(_render_transcript(selected)),),
                created_at=time.time(),
            )
            output = await complete(
                system,
                [transcript],
                max_tokens=self._config.summary_tokens + _LONG_TERM_OUTPUT_TOKENS,
                light=True,
            )
            summary, new_entries = _parse_output(output, self._config.summary_tokens)
            known = set(state.long_term)
            new_entries = [e for e in dict.fromkeys(new_entries) if e not in known]
            through_seq = state.active[cut - 1].seq

            def commit(conn: sqlite3.Connection) -> None:
                now = time.time()
                with _transaction(conn):
                    conn.execute(
                        "INSERT INTO summaries (conversation_id, through_seq, text, created_at)"
                        " VALUES (?, ?, ?, ?)",
                        (conversation_id, through_seq, summary, now),
                    )
                    conn.executemany(
                        "INSERT INTO long_term (mode, text, source_conversation_id, created_at)"
                        " VALUES (?, ?, ?, ?)",
                        [(state.mode.value, e, conversation_id, now) for e in new_entries],
                    )

            await self._run(commit)

    async def search(
        self,
        query: str,
        *,
        mode: Mode | None = None,
        conversation_id: ConversationId | None = None,
        limit: int = 5,
    ) -> Sequence[ArchiveHit]:
        words = _WORD_RE.findall(query)
        if not words or limit <= 0:
            return []
        match = " OR ".join(f'"{w}"' for w in dict.fromkeys(words))
        sql = (
            "SELECT m.conversation_id, m.id, m.role, m.created_at, m.blocks,"
            " bm25(messages_fts) AS rank"
            " FROM messages_fts JOIN messages m ON m.seq = messages_fts.rowid"
            " JOIN conversations c ON c.id = m.conversation_id"
            " WHERE messages_fts MATCH ?"
        )
        params: list[Any] = [match]
        if mode is not None:
            sql += " AND c.mode = ?"
            params.append(mode.value)
        if conversation_id is not None:
            sql += " AND m.conversation_id = ?"
            params.append(conversation_id)
        sql += " ORDER BY rank, m.seq LIMIT ?"
        params.append(limit)

        def run(conn: sqlite3.Connection) -> list[ArchiveHit]:
            return [
                ArchiveHit(row[0], _message(row[1:5]), -row[5])
                for row in conn.execute(sql, params)
            ]

        return await self._run(run)

    async def inspect(self, conversation_id: ConversationId) -> MemoryView:
        state = await self._run(_read_state, conversation_id, False)
        return MemoryView(summary=state.summary, long_term=state.long_term)

    async def aclose(self) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.close()
            self._conn = None

        if self._conn is not None:
            await self._run(run)
        self._executor.shutdown(wait=False)

    # ── Not in the Memory contract yet (see docs/memory-issues.md) ───────

    async def history(self, conversation_id: ConversationId) -> Sequence[Message]:
        """The full archived transcript in append order."""

        def run(conn: sqlite3.Connection) -> list[Message]:
            with _transaction(conn, "DEFERRED"):
                _require_conversation(conn, conversation_id)
                rows = conn.execute(
                    "SELECT id, role, created_at, blocks FROM messages"
                    " WHERE conversation_id = ? ORDER BY seq",
                    (conversation_id,),
                ).fetchall()
            return [_message(row) for row in rows]

        return await self._run(run)

    # ── Internals ─────────────────────────────────────────────────────────

    async def _run[T](self, fn: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(self._call, fn, *args))

    def _call[T](self, fn: Callable[..., T], *args: Any) -> T:
        if self._conn is None:
            self._conn = _open(self._path)
        return fn(self._conn, *args)


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        with _transaction(conn):
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                # executescript() would commit the open transaction; run statements singly.
                for statement in _SCHEMA.split(";\n"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"{path}: memory schema version {version} is not supported"
                    f" (expected {SCHEMA_VERSION})"
                )
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def _transaction(conn: sqlite3.Connection, kind: str = "IMMEDIATE") -> Iterator[None]:
    conn.execute(f"BEGIN {kind}")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _require_conversation(conn: sqlite3.Connection, conversation_id: ConversationId) -> Mode:
    row = conn.execute(
        "SELECT mode FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown conversation {conversation_id}")
    return Mode(row[0])


def _conversation_info(row: Sequence[Any]) -> ConversationInfo:
    return ConversationInfo(row[0], Mode(row[1]), row[2], row[3], row[4])


def _message(row: Sequence[Any]) -> Message:
    return Message(row[0], Role(row[1]), codec.decode_blocks(row[3]), row[2])


def _read_state(
    conn: sqlite3.Connection, conversation_id: ConversationId, with_active: bool = True
) -> _State:
    with _transaction(conn, "DEFERRED"):
        mode = _require_conversation(conn, conversation_id)
        row = conn.execute(
            "SELECT through_seq, text FROM summaries WHERE conversation_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        through_seq, summary = row if row else (0, "")
        long_term = [
            r[0]
            for r in conn.execute(
                "SELECT text FROM long_term WHERE mode = ? ORDER BY id", (mode.value,)
            )
        ]
        rows = (
            conn.execute(
                "SELECT seq, id, role, created_at, blocks FROM messages"
                " WHERE conversation_id = ? AND seq > ? ORDER BY seq",
                (conversation_id, through_seq),
            ).fetchall()
            if with_active
            else []
        )
    active = [_Active(r[0], _message(r[1:]), _estimate(r[4])) for r in rows]
    return _State(mode, summary, long_term, active)


def _estimate(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 4


def _valid_starts(messages: Sequence[Message], *, pending_calls_block: bool) -> list[int]:
    """Indices i where messages[i:] is a tail that orphans no tool result.

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
    for i in range(n + 1):
        depth += blocked[i]
        if depth == 0:
            starts.append(i)
    return starts


def _choose_cut(active: Sequence[_Active], config: CompactionConfig) -> int | None:
    total = sum(a.tokens for a in active)
    if total < config.trigger_tokens:
        return None
    starts = _valid_starts([a.message for a in active], pending_calls_block=True)
    candidates = [s for s in starts if 1 <= s < len(active)]
    if not candidates:
        return None
    target = total * config.remove_fraction
    removed, cumulative = 0, [0]
    for a in active:
        removed += a.tokens
        cumulative.append(removed)
    return next((s for s in candidates if cumulative[s] >= target), candidates[-1])


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
                case ToolCall(_, name, arguments):
                    lines.append(f"[{stamp} {message.role} calls {name}] {arguments}")
                case ToolResult(_, status, content):
                    lines.append(f"[{stamp} tool result, {status}] {content}")
                case _:
                    lines.append(f"[{stamp} {message.role}] {block}")
    return "\n".join(lines)


def _parse_output(output: str, summary_tokens: int) -> tuple[str, list[str]]:
    summary_match = _SUMMARY_RE.search(output)
    summary = (summary_match.group(1) if summary_match else output).strip()
    limit = summary_tokens * CHARS_PER_TOKEN
    if len(summary) > limit:
        cut = summary[:limit]
        summary = cut.rsplit(None, 1)[0] if " " in cut.strip() else cut
    entries: list[str] = []
    if summary_match and (long_term_match := _LONG_TERM_RE.search(output)):
        for line in long_term_match.group(1).splitlines():
            entry = line.strip().removeprefix("-").strip()
            if entry:
                entries.append(entry)
    return summary, entries


def _render_addendum(summary: str, long_term: Sequence[str]) -> str:
    parts = []
    if summary:
        parts.append(f"## Conversation summary\n{summary}")
    if long_term:
        parts.append("## Long-term memory\n" + "\n".join(f"- {e}" for e in long_term))
    return "\n\n".join(parts)


def _addendum_message(addendum: str) -> list[Message]:
    return [Message("memory-addendum", Role.USER, (Text(addendum),), 0.0)] if addendum else []


def _fit(state: _State, request: ContextRequest, long_term_tokens: int) -> AssembledContext:
    """Fit summary, then long-term entries, then the newest verbatim tail into the budget.

    The addendum is counted as one extra user message, since TokenCounter counts messages.
    """
    count, budget = request.count_tokens, request.max_tokens

    def fits(addendum: str, tail: Sequence[Message] = ()) -> bool:
        return count([*_addendum_message(addendum), *tail]) <= budget

    entries = list(state.long_term)
    used, keep = 0, 0
    for entry in reversed(entries):
        used += _estimate(entry)
        if used > long_term_tokens:
            break
        keep += 1
    entries = entries[len(entries) - keep :]

    keep = _max_true(
        len(entries), lambda k: fits(_render_addendum(state.summary, entries[len(entries) - k :]))
    )
    entries = entries[len(entries) - keep :]
    summary = state.summary
    if not fits(_render_addendum(summary, entries)):
        length = _max_true(len(summary), lambda n: fits(_render_addendum(summary[:n], [])))
        summary = summary[:length]
    addendum = _render_addendum(summary, entries)

    messages = [a.message for a in state.active]
    starts = _valid_starts(messages, pending_calls_block=False)
    # Smallest valid start whose tail fits; starts[-1] == len(messages) (empty tail).
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if fits(addendum, messages[starts[mid] :]):
            hi = mid
        else:
            lo = mid + 1
    return AssembledContext(system_addendum=addendum, messages=messages[starts[lo] :])


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
