"""Findings of the review of K5031/Kayden#1, each a test that failed before its fix."""

from __future__ import annotations

import fcntl
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from kayden.memory.sqlite import SqliteMemory
from kayden.runtime import InstanceLockedError, Runtime, RuntimeConfig
from tests.conftest import FAST, Harness
from tests.fakes.core import ScriptedCore
from tests.fakes.frontend import ScriptedFrontend
from tests.fakes.memory import MemoryStore
from tests.fakes.tools import FakeTools
from tests.test_memory_sqlite import schema_2_store


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
