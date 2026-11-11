"""Each conversation has its own work folder, handed to the Tools when it is opened."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kayden.api import SettingsUpdate, StartOptions
from kayden.runtime.config import ModuleState
from tests.conftest import Harness
from tests.fakes.tools import FakeTools


def folder_tools() -> FakeTools:
    tools = FakeTools()
    tools.folders = []
    return tools


@pytest.fixture
def folders(tmp_path: Path) -> dict[str, Path]:
    found = {name: tmp_path / name for name in ("default", "project", "other")}
    for path in found.values():
        path.mkdir()
    return found


async def harness(make: Any, folders: dict[str, Path], tools: FakeTools | None = None,
                  **config: Any) -> Harness:
    return await make(tools=tools or folder_tools(),
                      modules=ModuleState(work_folder=str(folders["default"])), **config)


async def test_a_new_conversation_gets_the_default_folder_and_keeps_it(make, folders):
    h = await harness(make, folders)
    first = await h.start()
    await h.runtime.rename_conversation(first.id, "First")  # stored: a new chat is not
    assert first.folder == str(folders["default"])
    assert h.tools.folders == [folders["default"]]
    await h.runtime.update_settings(SettingsUpdate(work_folder=str(folders["other"])))
    assert not (await h.runtime.settings()).restart_needed  # the default applies live
    second = await h.start()
    assert second.folder == str(folders["other"])
    reopened = await h.start(first.id)
    assert reopened.folder == str(folders["default"])  # unchanged by the new default
    assert h.tools.folders == [folders["default"], folders["other"], folders["default"]]


async def test_a_new_conversation_can_get_another_folder(make, folders):
    h = await harness(make, folders)
    info = await h.start(folder=str(folders["project"]))
    assert info.folder == str(folders["project"].resolve())
    assert h.tools.folders == [folders["project"].resolve()]
    await h.runtime.rename_conversation(info.id, "Project")
    [listed] = await h.runtime.list_conversations()
    assert listed.folder == info.folder


async def test_continue_from_keeps_the_source_folder(make, folders):
    h = await harness(make, folders)
    source = await h.start(folder=str(folders["project"]))
    await h.runtime.rename_conversation(source.id, "Source")
    seeded = await h.start(seed_from=source.id)
    assert seeded.folder == source.folder
    elsewhere = await h.start(seed_from=source.id, folder=str(folders["other"]))
    assert elsewhere.folder == str(folders["other"].resolve())


async def test_a_conversation_without_a_folder_uses_the_tools_default(make, folders):
    h = await harness(make, folders)
    old = await h.memory.create_conversation()  # stored before folders existed
    await h.start(old.id)
    assert h.tools.folders == [None]


@pytest.mark.parametrize("options, message", [
    ({"conversation_id": "x", "folder": "/tmp"}, "new conversation only"),
    ({"folder": "/no/such/folder"}, "is not a folder"),
])
async def test_invalid_folders_are_refused(make, folders, options, message):
    h = await harness(make, folders)
    with pytest.raises(ValueError, match=message):
        await h.runtime.start(StartOptions(**options))
    assert h.tools.folders == [] and await h.runtime.list_conversations() == []


async def test_a_folder_the_tools_cannot_use_is_refused(make, folders):
    tools = folder_tools()
    tools.folder_error = PermissionError("read-only file system")
    h = await harness(make, folders, tools=tools)
    with pytest.raises(ValueError, match="cannot be used: read-only file system"):
        await h.start()
    assert await h.runtime.list_conversations() == []


async def test_tools_with_a_fixed_folder(make, folders):
    h = await harness(make, folders, tools=FakeTools())
    info = await h.start()
    assert info.folder is None
    with pytest.raises(ValueError, match="one fixed folder"):
        await h.start(folder=str(folders["project"]))
