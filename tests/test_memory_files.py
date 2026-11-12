"""Claude Code-style memory (kayden.memory.files): the shared Memory conformance suite, and
what is its own: a folder of files per project that the agent writes with the memory tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from kayden.api import InstancePaths, ToolStatus
from kayden.memory.files import FilesMemory, factory
from kayden.memory.files.folder import RULES
from kayden.memory.sqlite import SqliteMemory
from tests.conformance.memory_suite import (
    FakeCompletion,
    MemoryConformance,
    context,
    count_tokens,
    text_message,
)
from tests.test_memory_sqlite import CONFIG, tagged


class TestFilesMemory(MemoryConformance):
    @pytest.fixture
    def memory_factory(self, tmp_path: Path):
        return lambda: FilesMemory(tmp_path / "memory.db", CONFIG, projects=tmp_path / "p")

    @pytest.fixture
    def completion(self) -> FakeCompletion:
        return FakeCompletion(render=tagged)


class Context:
    """What the memory tool needs of a call's context."""

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.call_id = "c"


async def test_factory_keeps_its_own_archive_and_a_folder_per_project(tmp_path: Path):
    memory = factory({}, InstancePaths(tmp_path))
    try:
        await memory.create_conversation(folder="/home/me/Code")
        assert (tmp_path / "memory-files.db").exists()
        assert not (tmp_path / "memory.db").exists()  # the built-in Memory's stays apart
        await memory.save_note("MEMORY.md", "", "- x\n", folder="/home/me/Code")
        assert (tmp_path / "projects" / "-home-me-Code" / "memory" / "MEMORY.md").exists()
    finally:
        await memory.aclose()


async def test_every_request_sees_the_rules_and_the_index_not_the_files(tmp_path: Path):
    memory = FilesMemory(tmp_path / "m.db", CONFIG, projects=tmp_path / "p")
    project = str(tmp_path / "project")
    conv = await memory.create_conversation(folder=project)
    other = await memory.create_conversation()
    for c in (conv, other):
        await memory.append(c.id, [text_message("tok0001")])
    await memory.save_note("MEMORY.md", "", "- [Tea](tea.md) — drinks tea\n", folder=project)
    await memory.save_note("tea.md", "Drinks tea", "Only green tea.", folder=project)
    added = (await memory.assemble(context(conv.id, 200_000))).system_addendum
    assert RULES in added and "- [Tea](tea.md) — drinks tea" in added
    assert "Only green tea." not in added
    elsewhere = (await memory.assemble(context(other.id, 200_000))).system_addendum
    assert "drinks tea" not in elsewhere and "(empty: nothing saved yet)" in elsewhere
    budget = 6_000  # the index from the top within the long-term budget
    index = "".join(f"- [Memory {i}](m{i}.md) — {'x' * 100}\n" for i in range(100))
    await memory.save_note("MEMORY.md", "", index, folder=project)
    later = await memory.create_conversation(folder=project)
    await memory.append(later.id, [text_message("tok0002")])
    added = (await memory.assemble(context(later.id, budget))).system_addendum
    kept = [line for line in added.splitlines() if line.startswith("- [Memory")]
    assert kept[0].startswith("- [Memory 0]") and len(kept) < 100
    assert count_tokens("\n".join(kept)) <= CONFIG.long_term_fraction * budget + 10
    await memory.aclose()


async def test_a_conversation_keeps_the_index_it_started_with(tmp_path: Path):
    """As in Claude Code, memory is loaded when a conversation starts: a memory saved
    meanwhile changes nothing at the start of its prompt, which keeps the provider's cache
    valid. A later conversation, or this one once it is summarized, sees the new index."""
    memory = FilesMemory(tmp_path / "m.db", CONFIG, projects=tmp_path / "p")
    project = str(tmp_path / "project")
    conv = await memory.create_conversation(folder=project)
    await memory.append(conv.id, [text_message("tok0001")])
    await memory.save_note("MEMORY.md", "", "- [Tea](tea.md) — drinks tea\n", folder=project)
    first = (await memory.assemble(context(conv.id, 200_000))).system_addendum
    await memory.save_note("MEMORY.md", "", "- [Tea](tea.md) — drinks tea\n"
                                            "- [Cat](cat.md) — has a cat\n", folder=project)
    assert (await memory.assemble(context(conv.id, 200_000))).system_addendum == first
    assert "has a cat" not in " ".join((await memory.inspect(conv.id)).long_term)
    later = await memory.create_conversation(folder=project)
    await memory.append(later.id, [text_message("tok0002")])
    assert "has a cat" in (await memory.assemble(context(later.id, 200_000))).system_addendum
    summarized = await memory._long_term(conv, "What happened so far.")
    assert any("has a cat" in line for line in summarized.lines)
    await memory.aclose()


async def test_a_note_is_a_file_with_claude_codes_frontmatter(tmp_path: Path):
    memory = FilesMemory(tmp_path / "m.db", CONFIG, projects=tmp_path / "p")
    note = await memory.save_note("user-tea", "Drinks tea", "Only green tea.")
    assert note.id == note.title == "user-tea.md"
    file = tmp_path / "p" / "default" / "memory" / "user-tea.md"
    assert file.read_text() == ("---\nname: user-tea\ndescription: Drinks tea\nmetadata:\n"
                                "  type: user\n---\n\nOnly green tea.\n")
    edited = await memory.save_note("user-tea.md", "", "Matcha too.", note_id=note.id)
    assert edited.description == "Drinks tea" and edited.text == "Matcha too.\n"
    await memory.aclose()


async def test_the_memory_tool_works_on_the_conversations_project(tmp_path: Path):
    from kayden.api import ToolCall

    memory = FilesMemory(tmp_path / "m.db", CONFIG, projects=tmp_path / "p")
    project = str(tmp_path / "project")
    conv = await memory.create_conversation(folder=project)
    names = [t.name for t in await memory.list_tools()]
    assert names == ["recall", "read_around", "memory"]

    async def run(**args):
        return await memory.execute(ToolCall("c", "memory", args), Context(conv.id))

    assert not (await run(command="create", path="a.md", file_text="one\ntwo\n")).is_error
    assert not (await run(command="str_replace", path="a.md", old_str="two",
                          new_str="2")).is_error
    assert not (await run(command="insert", path="a.md", insert_line=1,
                          insert_text="one and a half")).is_error
    view = await run(command="view", path="a.md")
    assert "one and a half" in view.content and "\t2" in view.content
    assert not (await run(command="rename", old_path="a.md", new_path="b.md")).is_error
    assert (await run(command="view", path="")).content.startswith("b.md")
    assert (await run(command="str_replace", path="b.md", old_str="x", new_str="y")).is_error
    assert (await run(command="create", path="../out.md", file_text="x")).is_error
    assert not (await run(command="delete", path="b.md")).is_error
    assert (await run(command="view", path="b.md")).is_error
    assert await memory.notes(folder=project) == []
    await memory.aclose()


async def test_the_built_in_memorys_store_is_not_this_ones(tmp_path: Path):
    """A different Memory is a different archive: the built-in one's file is refused."""
    built_in = SqliteMemory(tmp_path / "memory.db", CONFIG)
    await built_in.create_conversation()
    await built_in.aclose()
    files = FilesMemory(tmp_path / "memory.db", CONFIG, projects=tmp_path / "p")
    with pytest.raises(RuntimeError, match="schema version"):
        await files.create_conversation()
    await files.aclose()



async def test_the_runtime_runs_on_it_unchanged(make, tmp_path: Path):
    """The modularity check: the same runtime, another Memory. The agent saves with the
    memory tool, the apps hear of it, and the next request sees the index."""
    from kayden.api import MemoriesChanged, PermissionPolicy
    from tests.fakes.core import ScriptedCore, Step, call
    from tests.fakes.tools import FakeTools

    tools = FakeTools()
    tools.folders = []  # takes a work folder, as the shell and file tools do
    memory = FilesMemory(tmp_path / "m.db", CONFIG, projects=tmp_path / "p")
    memo = ("---\nname: user-tea\ndescription: Drinks green tea\nmetadata:\n  type: user\n"
            "---\n\nOnly green tea.\n")
    h = await make(memory=memory, tools=tools, permissions=PermissionPolicy.DENY,
                   core=ScriptedCore([
                       Step(calls=[call("memory", {"command": "create", "path": "user-tea.md",
                                                   "file_text": memo}, id="c"),
                                   call("memory", {"command": "create", "path": "MEMORY.md",
                                                   "file_text": "- [Tea](user-tea.md) — tea\n"},
                                        id="i")]),
                       Step(text="Noted."), Step(text="Green tea, then."),
                       Step(text="Green tea.")],
                                        maintenance=Step(text="Tea")))
    project = tmp_path / "project"
    project.mkdir()
    await h.start(folder=str(project))
    await h.turn("I only drink green tea")
    assert {r.status for r in (await h.results()).values()} == {ToolStatus.OK}
    assert h.rec.of(MemoriesChanged)
    notes = {n.title: n for n in await h.runtime.notes()}
    assert list(notes) == ["MEMORY.md", "user-tea.md"]
    assert notes["user-tea.md"].description == "Drinks green tea"
    turns = [r for r in h.core.requests if not r.maintenance]
    await h.turn("what do I drink?")
    # The same conversation: its prompt is as it was (the agent's own calls show what it
    # saved), so the provider's cache stays valid.
    assert h.core.requests[-1].system == turns[0].system
    await h.start(folder=str(project))  # a later conversation in the project
    await h.turn("and what do I drink?")
    system = h.core.requests[-1].system
    assert "- [Tea](user-tea.md) — tea" in system and "Only green tea." not in system
