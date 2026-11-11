"""The tools Memory offers: recall and read_around on the archive, and memory, which reads and
writes the project's memory folder as Claude Code does."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kayden.api import BUILTIN_TOOLS, MemoriesChanged, PermissionPolicy, ToolStatus
from tests.conformance.memory_suite import text_message
from tests.conftest import Harness
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools


async def harness(make: Any, *steps: Step, **memory_kw: Any) -> Harness:
    return await make(core=ScriptedCore([*steps, Step(text="done")]),
                      permissions=PermissionPolicy.DENY, memory_kw=memory_kw)


def folder_tools() -> FakeTools:
    tools = FakeTools()
    tools.folders = []  # takes a work folder, as the shell and file tools do
    return tools


async def chat_in(make: Any, steps: list[Step], folder: Path) -> Harness:
    h: Harness = await make(core=ScriptedCore([*steps, Step(text="done")]),
                            permissions=PermissionPolicy.DENY, tools=folder_tools())
    await h.chat("Work", folder=str(folder))
    return h


async def test_the_agent_saves_a_memory_and_its_index_like_claude_code(make: Any,
                                                                        tmp_path: Path) -> None:
    memo = ("---\nname: user-tea\ndescription: The user drinks green tea\nmetadata:\n"
            "  type: user\n---\n\nDrinks green tea.\n")
    h = await chat_in(make, [
        Step(calls=[call("memory", {"command": "create", "path": "user-tea.md",
                                    "file_text": memo}, id="c"),
                    call("memory", {"command": "create", "path": "MEMORY.md",
                                    "file_text": "- [Tea](user-tea.md) — green tea\n"},
                         id="i")])], tmp_path)
    await h.turn("I only drink green tea")
    results = await h.results()
    assert results["c"].status == ToolStatus.OK and results["i"].status == ToolStatus.OK
    files = {f.path: f.text for f in await h.runtime.memory_files()}
    assert list(files) == ["MEMORY.md", "user-tea.md"] and files["user-tea.md"] == memo
    assert h.rec.of(MemoriesChanged)  # the Memory screens refresh
    assert not h.tools.started  # a Memory tool: runs under DENY, never gated
    # The next request sees the index, under the rules, but not the memory itself.
    system = h.core.requests[-1].system
    assert "- [Tea](user-tea.md) — green tea" in system and "MEMORY.md" in system
    assert "Drinks green tea." not in system


async def test_memory_commands_view_edit_insert_rename_and_delete(make: Any,
                                                                   tmp_path: Path) -> None:
    h = await chat_in(make, [Step(calls=[
        call("memory", {"command": "create", "path": "a.md", "file_text": "one\ntwo\n"},
             id="create"),
        call("memory", {"command": "str_replace", "path": "a.md", "old_str": "two",
                        "new_str": "2"}, id="replace"),
        call("memory", {"command": "insert", "path": "a.md", "insert_line": 1,
                        "insert_text": "one and a half"}, id="insert"),
        call("memory", {"command": "view", "path": "a.md"}, id="view"),
        call("memory", {"command": "rename", "old_path": "a.md", "new_path": "b.md"},
             id="rename"),
        call("memory", {"command": "view", "path": ""}, id="list"),
        call("memory", {"command": "str_replace", "path": "b.md", "old_str": "absent",
                        "new_str": "x"}, id="missing"),
        call("memory", {"command": "create", "path": "../escape.md", "file_text": "x"},
             id="escape"),
        call("memory", {"command": "delete", "path": "b.md"}, id="delete"),
        call("memory", {"command": "view", "path": "b.md"}, id="gone")])], tmp_path)
    await h.turn("go")
    r = await h.results()
    assert "one and a half" in r["view"].content and "\t2" in r["view"].content
    assert r["list"].content.startswith("b.md")
    assert r["missing"].status == ToolStatus.ERROR and "occurs 0 times" in r["missing"].content
    assert r["escape"].status == ToolStatus.ERROR
    assert r["gone"].status == ToolStatus.ERROR
    assert await h.runtime.memory_files() == []


async def test_each_project_has_its_own_memory(make: Any, tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(), two.mkdir()
    h: Harness = await make(core=ScriptedCore([Step(text="ok")]), tools=folder_tools())
    await h.chat("One", folder=str(one))
    await h.runtime.write_memory_file("MEMORY.md", "- one\n")
    await h.chat("Two", folder=str(two))
    assert await h.runtime.memory_files() == []
    await h.chat("Back in one", folder=str(one))
    assert [f.text for f in await h.runtime.memory_files()] == ["- one\n"]


async def test_only_recall_when_the_memory_module_keeps_no_files(make: Any) -> None:
    h = await harness(make, editable=False)
    await h.start()
    names = {spec.name for spec in await h.runtime.list_tools()}
    assert {"recall", "read_around"} <= names and "memory" not in names


def test_they_come_from_memory_not_the_runtime() -> None:
    assert not {"recall", "read_around", "memory"} & set(BUILTIN_TOOLS)


async def test_a_memory_without_tools_gets_none(make: Any) -> None:
    h = await harness(make)

    async def no_tools() -> tuple[()]:
        return ()

    h.memory.list_tools = no_tools  # type: ignore[method-assign]
    await h.start()
    names = {spec.name for spec in await h.runtime.list_tools()}
    assert not names & {"recall", "memory"} and "op_status" in names


async def test_read_around_shows_a_hit_with_the_messages_before_and_after(make: Any) -> None:
    h: Harness = await make(core=ScriptedCore([]), permissions=PermissionPolicy.DENY)
    first = await h.chat()
    for text in ("We need a name.", "How about Kayden?", "Yes, do that.", "Done."):
        await h.memory.append(first.id, [text_message(text)])
    messages = await h.memory.history(first.id)
    hit = messages[2]  # "Yes, do that.": meaningless without what came before
    h.core.add(Step(calls=[call("read_around", {"message_id": hit.id, "before": 1,
                                                "after": 1}, id="a"),
                           call("read_around", {"message_id": messages[0].id}, id="b"),
                           call("read_around", {"message_id": "nope"}, id="c")]),
               Step(text="ok"))
    await h.turn("what did we call it?")
    results = await h.results()
    around = results["a"].content
    assert around.index("How about Kayden?") < around.index("Yes, do that.") < around.index(
        "Done.")
    assert "We need a name." not in around  # one before, one after
    assert around.count("→") == 1 and "→ [" in around.split("Yes, do that.")[0].splitlines()[-1]
    assert "We need a name." in results["b"].content  # the first message: nothing before
    assert results["c"].status == ToolStatus.ERROR
