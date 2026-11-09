"""Findings of the review of K5031/Kayden#1, each a test that failed before its fix."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from kayden.api import Message, Role, Text, ToolCall, ToolResult, ToolStatus
from kayden.memory.sqlite import SqliteMemory, codec
from kayden.runtime import InstanceLockedError, Runtime, RuntimeConfig
from tests.conftest import FAST, Harness
from tests.fakes.core import ScriptedCore
from tests.fakes.frontend import ScriptedFrontend
from tests.fakes.memory import MemoryStore
from tests.fakes.tools import FakeTools
from tests.test_memory_sqlite import SCHEMA_2, schema_2_store


# F1: a second process that could not start (instance locked) could still delete the
# conversation the first process has open.
async def test_second_process_cannot_delete_the_open_conversation(make: Any, tmp_path: Path):
    store = MemoryStore()
    data_dir = tmp_path / "shared"
    a: Harness = await make(store=store, data_dir=data_dir)
    b: Harness = await make(store=store, data_dir=data_dir)
    conv = await a.start()
    await a.turn("hello")
    with pytest.raises(InstanceLockedError):
        await b.start()
    with pytest.raises(Exception):  # noqa: B017 - any refusal will do
        await b.runtime.delete_conversation(conv.id)
    assert conv.id in store.conversations


# F2: the schema 3 migration ran on the first Memory call, before start() took the
# instance lock, so it migrated while an older Kayden still held session.lock. Every Memory
# call now needs the lock first.
async def test_migration_waits_for_the_instance_lock(tmp_path: Path, clean_env: Any):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = data_dir / "memory.db"
    schema_2_store(db)
    fd = os.open(data_dir / "session.lock", os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # an older Kayden is running
    try:
        runtime = Runtime(core=ScriptedCore(), memory=SqliteMemory(db), tools=FakeTools(),
                          frontend=ScriptedFrontend().info,
                          config=RuntimeConfig(data_dir=data_dir, **FAST))
        try:
            with pytest.raises(InstanceLockedError):
                await runtime.list_conversations()  # the start screen fills its pickers
            with pytest.raises(InstanceLockedError):
                from kayden.api import StartOptions
                await runtime.start(StartOptions())
        finally:
            await runtime.aclose()
        with sqlite3.connect(db) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        os.close(fd)


def _v1_search_text(raw: str) -> str:
    """searchable_text as schema 1 wrote it (ddb5a1e^): every string, ids included.
    The v1 to v2 migration rewrote blocks but never rebuilt messages_fts."""
    parts: list[str] = []
    for item in json.loads(raw):
        data = item["data"]
        parts.extend(codec._strings(data.get("text") if item["type"] == "reasoning" else data))
    return "\n".join(parts)


# F3: rows indexed by a schema 1 store kept their tool-call ids and raw arguments in
# the FTS index after delete_conversation, which removes only the current text.
async def test_deleted_v1_era_rows_leave_nothing_in_the_index(tmp_path: Path):
    db = tmp_path / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA_2)
        conn.execute("INSERT INTO conversations VALUES ('s1', 'session', NULL, 1, 2)")
        messages = [
            Message("a", Role.USER, (Text("write it"),), 1.0),
            Message("b", Role.ASSISTANT, (ToolCall("callxyzzy", "write_file",
                                                    {"text": "hunter"},
                                                    '{"text": "plughsecret"}'),), 2.0),
            Message("c", Role.TOOL, (ToolResult("callxyzzy", ToolStatus.OK, "done"),), 3.0),
        ]
        for m in messages:
            blocks = codec.encode_blocks(m.blocks)
            cur = conn.execute(
                "INSERT INTO messages (conversation_id, id, role, created_at, format, blocks,"
                " archived_at) VALUES ('s1', ?, ?, ?, ?, ?, 9)",
                (m.id, m.role.value, m.created_at, codec.FORMAT_VERSION, blocks))
            conn.execute("INSERT INTO messages_fts (rowid, body) VALUES (?, ?)",
                         (cur.lastrowid, _v1_search_text(blocks)))
    memory = SqliteMemory(db)
    try:
        await memory.delete_conversation("s1")
    finally:
        await memory.aclose()
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT count(*) FROM messages_fts_data").fetchone()[0]
        conn.execute("CREATE VIRTUAL TABLE v USING fts5vocab(messages_fts, 'row')")
        terms = {r[0] for r in conn.execute("SELECT term FROM v")}
    assert terms == set(), (terms, rows)
    assert b"plughsecret" not in db.read_bytes() and b"callxyzzy" not in db.read_bytes()
