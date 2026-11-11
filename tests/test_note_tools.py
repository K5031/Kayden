"""The note tools Memory offers: the agent reads, saves and forgets long-term memory notes."""

from __future__ import annotations

from typing import Any

from kayden.api import BUILTIN_TOOLS, PROFILE, MemoriesChanged, PermissionPolicy, ToolStatus
from kayden.memory.tools import SAVE_NOTE
from tests.conformance.memory_suite import text_message
from tests.conftest import Harness
from tests.fakes.core import ScriptedCore, Step, call


async def harness(make: Any, *steps: Step, **memory_kw: Any) -> Harness:
    return await make(core=ScriptedCore([*steps, Step(text="done")]),
                      permissions=PermissionPolicy.DENY, memory_kw=memory_kw)


async def test_save_note_writes_a_note_from_this_conversation(make: Any) -> None:
    h = await harness(make, Step(calls=[call("save_note", {
        "title": "Diet", "description": "What the user eats", "text": "Is vegetarian."},
        id="s")]))
    conversation = await h.start()
    await h.turn("I'm vegetarian")
    result = (await h.results())["s"]
    assert result.status == ToolStatus.OK and "Saved 'Diet'" in result.content
    note = next(n for n in await h.runtime.notes() if n.title == "Diet")
    assert note.text == "Is vegetarian." and note.source_conversation_id == conversation.id
    assert h.rec.of(MemoriesChanged)  # the Memory screens refresh
    assert not h.tools.started  # a Memory tool: runs under DENY, never gated


async def test_read_note_returns_its_text_and_lists_notes_when_missing(make: Any) -> None:
    h = await harness(make, Step(calls=[call("read_note", {"title": "diet"}, id="r"),
                                        call("read_note", {"title": "Music"}, id="m")]))
    await h.runtime.save_note("Diet", "What the user eats", "Is vegetarian.")
    await h.start()
    await h.turn("what do I eat?")
    results = await h.results()
    assert "Is vegetarian." in results["r"].content
    assert results["m"].status == ToolStatus.ERROR
    assert "No note is titled 'Music'" in results["m"].content and "- Diet" in results["m"].content


async def test_forget_note_deletes_it_but_not_the_profile(make: Any) -> None:
    h = await harness(make, Step(calls=[call("forget_note", {"title": "Diet"}, id="f"),
                                        call("forget_note", {"title": PROFILE}, id="p")]))
    await h.runtime.save_note("Diet", "What the user eats", "Is vegetarian.")
    await h.start()
    await h.turn("forget what I eat")
    results = await h.results()
    assert results["f"].status == ToolStatus.OK
    assert results["p"].status == ToolStatus.ERROR and "emptied" in results["p"].content
    assert [n.title for n in await h.runtime.notes()] == [PROFILE]


async def test_only_recall_when_the_memory_module_keeps_no_notes(make: Any) -> None:
    h = await harness(make, editable=False)
    await h.start()
    names = {spec.name for spec in await h.runtime.list_tools()}
    assert {"recall", "read_around"} <= names
    assert not names & {"read_note", "save_note", "forget_note"}


def test_they_come_from_memory_not_the_runtime() -> None:
    assert not {"recall", "read_note", "save_note", "forget_note"} & set(BUILTIN_TOOLS)
    assert "password" in SAVE_NOTE.description and "read it first" in SAVE_NOTE.description


async def test_a_memory_without_tools_gets_none(make: Any) -> None:
    h = await harness(make)

    async def no_tools() -> tuple[()]:
        return ()

    h.memory.list_tools = no_tools  # type: ignore[method-assign]
    await h.start()
    names = {spec.name for spec in await h.runtime.list_tools()}
    assert not names & {"recall", "save_note"} and "op_status" in names


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
