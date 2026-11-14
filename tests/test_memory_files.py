"""Claude Code-style memory (kadence.memory.files): the shared Memory conformance suite, and
what is its own: a folder of files per project that the agent writes with the memory tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from kadence.api import InstancePaths, ToolStatus
from kadence.memory.files import FilesMemory, factory
from kadence.memory.files.folder import RULES
from kadence.memory.sqlite import SqliteMemory
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
    written = file.read_text().splitlines()
    assert written[:5] == ["---", "name: user-tea", "description: Drinks tea", "metadata:",
                           "  type: user"]
    assert written[5].startswith("  modified: 20") and written[6:] == ["---", "",
                                                                      "Only green tea."]
    edited = await memory.save_note("user-tea.md", "", "Matcha too.", note_id=note.id)
    assert edited.description == "Drinks tea" and edited.text == "Matcha too.\n"
    await memory.aclose()


async def test_the_memory_tool_works_on_the_conversations_project(tmp_path: Path):
    from kadence.api import ToolCall

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


async def _tool(tmp_path: Path):
    """A files memory, a conversation in a project, and its memory tool as a function."""
    from kadence.api import ToolCall

    memory = FilesMemory(tmp_path / "m.db", CONFIG, projects=tmp_path / "p")
    project = str(tmp_path / "project")
    conv = await memory.create_conversation(folder=project)
    await memory.append(conv.id, [text_message("tok0001")])

    async def run(**args):
        return await memory.execute(ToolCall("c", "memory", args), Context(conv.id))

    return memory, project, conv, run


async def test_a_memory_is_read_before_it_is_changed(tmp_path: Path):
    """What the agent has not read in this conversation it cannot overwrite or edit: a
    memory another conversation or the user wrote stays as it is until it is looked at."""
    memory, project, _conv, run = await _tool(tmp_path)
    await memory.save_note("tea.md", "Drinks tea", "Only green tea.", folder=project)
    edit = {"command": "str_replace", "path": "tea.md", "old_str": "green", "new_str": "black"}
    for blind in (edit, {"command": "delete", "path": "tea.md"},
                  {"command": "insert", "path": "tea.md", "insert_line": 0, "insert_text": "x"},
                  {"command": "rename", "old_path": "tea.md", "new_path": "drinks.md"}):
        refused = await run(**blind)
        assert refused.is_error and "View tea.md" in refused.content
    over = await run(command="create", path="tea.md", file_text="Coffee now.")
    assert over.is_error and "exists" in over.content  # never a blind rewrite
    assert "green" in (await run(command="view", path="tea.md")).content
    assert not (await run(**edit)).is_error
    assert not (await run(**{**edit, "old_str": "black", "new_str": "oolong"})).is_error
    assert "oolong" in (await memory.notes(folder=project))[0].text
    await memory.aclose()


async def test_a_memory_changed_since_it_was_read_is_read_again(tmp_path: Path):
    memory, project, _conv, run = await _tool(tmp_path)
    await memory.save_note("tea.md", "Drinks tea", "Only green tea.", folder=project)
    await run(command="view", path="tea.md")
    await memory.save_note("tea.md", "Drinks tea", "Only black tea.", folder=project)  # the user
    stale = await run(command="str_replace", path="tea.md", old_str="tea.", new_str="tea!")
    assert stale.is_error and "changed since" in stale.content
    assert "black" in (await run(command="view", path="tea.md")).content
    assert not (await run(command="str_replace", path="tea.md", old_str="tea.",
                          new_str="tea!")).is_error
    await memory.aclose()


async def test_the_index_in_the_prompt_counts_as_read_while_it_is_current(tmp_path: Path):
    memory, project, conv, run = await _tool(tmp_path)
    await memory.save_note("MEMORY.md", "", "- [Tea](tea.md) — drinks tea\n", folder=project)
    await memory.assemble(context(conv.id, 200_000))  # every request shows it the index
    added = await run(command="insert", path="MEMORY.md", insert_line=1,
                      insert_text="- [Cat](cat.md) — has a cat")
    assert not added.is_error
    other = await memory.create_conversation(folder=project)
    await memory.append(other.id, [text_message("tok0002")])
    await memory.assemble(context(other.id, 200_000))
    await memory.save_note("MEMORY.md", "", "- [Dog](dog.md) — has a dog\n", folder=project)
    from kadence.api import ToolCall
    late = await memory.execute(ToolCall("c", "memory", {
        "command": "insert", "path": "MEMORY.md", "insert_line": 0, "insert_text": "- x"}),
        Context(other.id))
    assert late.is_error and "View MEMORY.md" in late.content  # not what its prompt showed
    await memory.aclose()


async def test_an_index_past_what_is_loaded_is_saved_and_reported(tmp_path: Path):
    """As in Claude Code: a conversation gets the first 200 lines or 25 kB of the index.
    A write past that stands, as an error telling the agent to rewrite it; near it, the
    write comes with a reminder."""
    from kadence.memory.files.folder import INDEX_MAX_BYTES, INDEX_MAX_LINES

    memory, project, _conv, run = await _tool(tmp_path)

    def index(count: int) -> str:
        return "".join(f"- [M{i}](m{i}.md) — fact {i}\n" for i in range(count))

    fine = await run(command="create", path="MEMORY.md", file_text=index(100))
    assert fine.content == "Saved MEMORY.md."
    await run(command="delete", path="MEMORY.md")
    near = await run(command="create", path="MEMORY.md", file_text=index(INDEX_MAX_LINES - 10))
    assert not near.is_error and "near its limit" in near.content and "merge" in near.content
    over = await run(command="insert", path="MEMORY.md", insert_line=0,
                     insert_text=index(20).rstrip())
    assert over.is_error and over.content.startswith("Saved, but")
    assert "Rewrite it now" in over.content
    saved = (await memory.notes(folder=project))[0].text.splitlines()
    assert len(saved) == INDEX_MAX_LINES + 10  # the write stands
    other = await memory.create_conversation(folder=project)
    await memory.append(other.id, [text_message("tok0002")])
    shown = (await memory.assemble(context(other.id, 500_000))).system_addendum
    assert len([line for line in shown.splitlines() if line.startswith("- [M")]) \
        == INDEX_MAX_LINES  # what is past the limit is not loaded
    await run(command="view", path="MEMORY.md")
    await run(command="delete", path="MEMORY.md")
    wide = await run(command="create", path="MEMORY.md",
                     file_text=f"- [Wide](w.md) — {'x' * INDEX_MAX_BYTES}\n")
    assert wide.is_error and "kB" in wide.content  # few lines, but too many bytes
    # A memory file has no limit of its own.
    assert not (await run(command="create", path="essay.md", file_text="x" * 50_000)).is_error
    await memory.aclose()


def test_a_git_repository_has_one_memory_for_all_its_folders_and_worktrees(tmp_path: Path):
    """As in Claude Code: the project is the repository, the main one for a worktree; a
    folder outside any repository is its own project."""
    import subprocess

    from kadence.memory.files.folder import project_key

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                       check=True, capture_output=True)

    repo = tmp_path / "repo"
    (repo / "src" / "deep").mkdir(parents=True)
    git("init", "-q", cwd=repo)
    (repo / "a.txt").write_text("a")
    git("add", ".", cwd=repo)
    git("commit", "-qm", "first", cwd=repo)
    tree = tmp_path / "tree"
    git("worktree", "add", "-q", str(tree), "-b", "side", cwd=repo)
    key = str(repo.resolve()).replace("/", "-")
    assert project_key(str(repo)) == key
    assert project_key(str(repo / "src" / "deep")) == key  # a folder inside it
    assert project_key(str(tree)) == key  # a worktree of it
    plain = tmp_path / "plain"
    plain.mkdir()
    assert project_key(str(plain)) == str(plain.resolve()).replace("/", "-")
    assert project_key(None) == "default"


def test_a_memory_file_records_when_it_was_written():
    from datetime import UTC, datetime

    from kadence.memory.files.folder import stamped

    now = datetime(2026, 11, 12, 10, 20, 30, 123000, tzinfo=UTC)
    typed = "---\nname: tea\ndescription: Tea\nmetadata:\n  type: user\n---\n\nGreen tea.\n"
    once = stamped(typed, now)
    assert once == ("---\nname: tea\ndescription: Tea\nmetadata:\n  type: user\n"
                    "  modified: 2026-11-12T10:20:30.123Z\n---\n\nGreen tea.\n")
    later = stamped(once, datetime(2026, 11, 13, tzinfo=UTC))  # replaced, not added again
    assert later.count("modified:") == 1 and "2026-11-13T00:00:00.000Z" in later
    bare = stamped("---\nname: tea\n---\nGreen tea.\n", now)
    assert bare == ("---\nname: tea\nmetadata:\n  modified: 2026-11-12T10:20:30.123Z\n---\n"
                    "Green tea.\n")
    assert stamped("No frontmatter here.\n", now) == "No frontmatter here.\n"


async def test_the_tool_and_the_user_both_stamp_a_memory_and_not_the_index(tmp_path: Path):
    memory, project, _conv, run = await _tool(tmp_path)
    text = "---\nname: tea\ndescription: Tea\nmetadata:\n  type: user\n---\n\nGreen tea.\n"
    await run(command="create", path="tea.md", file_text=text)
    await run(command="create", path="MEMORY.md", file_text="- [Tea](tea.md) — tea\n")
    viewed = (await run(command="view", path="tea.md")).content
    assert "modified: 20" in viewed
    # The agent goes on from what is stored, stamp included.
    assert not (await run(command="str_replace", path="tea.md", old_str="Green",
                          new_str="Oolong")).is_error
    await memory.save_note("cat.md", "Has a cat", "A grey cat.", folder=project)
    notes = {n.title: n for n in await memory.notes(folder=project)}
    assert notes["cat.md"].text == "A grey cat.\n" and notes["tea.md"].description == "Tea"
    folder = next((tmp_path / "p").iterdir()) / "memory"
    assert "modified:" in (folder / "cat.md").read_text()
    assert "modified" not in (folder / "MEMORY.md").read_text()
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
    from kadence.api import MemoriesChanged, PermissionPolicy
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
