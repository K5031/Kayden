"""The built-in remember and forget tools: the agent keeps and drops long-term memories."""

from __future__ import annotations

from typing import Any

from kayden.api import BUILTIN_TOOLS, MemoriesChanged, PermissionPolicy, ToolStatus
from tests.conftest import Harness
from tests.fakes.core import ScriptedCore, Step, call


async def harness(make: Any, *steps: Step, **memory_kw: Any) -> Harness:
    return await make(core=ScriptedCore([*steps, Step(text="done")]),
                      permissions=PermissionPolicy.DENY, memory_kw=memory_kw)


async def test_remember_adds_a_memory_from_this_conversation(make: Any) -> None:
    h = await harness(make, Step(calls=[call("remember", {"text": "  Is vegetarian. "},
                                             id="r")]))
    conversation = await h.start()
    await h.turn("I'm vegetarian")
    result = (await h.results())["r"]
    assert result.status == ToolStatus.OK and "Remembered" in result.content
    [entry] = await h.runtime.memories()
    assert entry.text == "Is vegetarian." and entry.source_conversation_id == conversation.id
    assert h.rec.of(MemoriesChanged)  # the Memory screens refresh
    assert not h.tools.started  # a built-in: runs under DENY, never gated


async def test_remember_does_not_keep_the_same_fact_twice(make: Any) -> None:
    h = await harness(make, Step(calls=[call("remember", {"text": "is VEGETARIAN"}, id="r")]))
    await h.runtime.add_memory("Is vegetarian")
    await h.start()
    await h.turn("I'm vegetarian")
    assert "Already remembered" in (await h.results())["r"].content
    assert len(await h.runtime.memories()) == 1


async def test_forget_removes_the_memory_with_that_text(make: Any) -> None:
    h = await harness(make, Step(calls=[call("forget", {"text": "lives in  oslo"}, id="f")]),
                      Step(calls=[call("forget", {"text": "Likes jazz"}, id="g")]))
    await h.runtime.add_memory("Lives in Oslo")
    await h.runtime.add_memory("Likes tea")
    await h.start()
    await h.turn("I moved away from Oslo")
    assert (await h.results())["f"].status == ToolStatus.OK
    missing = (await h.results())["g"]
    assert missing.status == ToolStatus.ERROR
    assert "No memory says that" in missing.content and "- Likes tea" in missing.content
    assert [e.text for e in await h.runtime.memories()] == ["Likes tea"]


async def test_an_empty_memory_is_refused(make: Any) -> None:
    h = await harness(make, Step(calls=[call("remember", {"text": "   "}, id="r")]))
    await h.start()
    await h.turn("hi")
    assert (await h.results())["r"].status == ToolStatus.ERROR
    assert await h.runtime.memories() == []


async def test_not_offered_when_the_memory_module_cannot_edit_memories(make: Any) -> None:
    h = await harness(make, editable=False)
    await h.start()
    names = {spec.name for spec in await h.runtime.list_tools()}
    assert "recall" in names and not names & {"remember", "forget"}


def test_they_are_built_ins() -> None:
    assert {"remember", "forget"} <= set(BUILTIN_TOOLS)
    assert "password" in BUILTIN_TOOLS["remember"].description
