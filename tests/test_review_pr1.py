"""Findings of the review of K5031/Kadence#1, each a test that failed before its fix."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kadence.api import InstanceLockedError
from tests.conftest import Harness
from tests.fakes.memory import MemoryStore


# F1: a second process that could not start (instance locked) could still delete the
# conversation the first process has open.
async def test_second_process_cannot_delete_the_open_conversation(make: Any, tmp_path: Path):
    store = MemoryStore()
    data_dir = tmp_path / "shared"
    a: Harness = await make(store=store, data_dir=data_dir)
    conv = await a.start()
    await a.turn("hello")
    with pytest.raises(InstanceLockedError):  # no second runtime to delete with
        await make(store=store, data_dir=data_dir)
    assert conv.id in store.conversations


