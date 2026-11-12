"""The example modules of docs/writing-modules.md work in a real runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from examples.dice_tools import DiceTools
from examples.echo_core import EchoCore
from examples.line_frontend import LineFrontend
from examples.ram_memory import RamMemory
from kayden.api import Message, Role, Text, ToolStatus
from tests.fakes.core import ScriptedCore, Step, call


async def test_the_example_tools_are_offered_and_run(make):
    h = await make(more_tools=[DiceTools()], core=ScriptedCore([
        Step(calls=[call("roll_dice", {"sides": 6, "count": 3}, id="r"),
                    call("roll_dice", {"sides": 1}, id="bad")]),
        Step(text="Done.")]))
    await h.start()
    await h.turn("roll three dice")
    assert "roll_dice" in {t.name for t in h.core.requests[0].tools}
    results = await h.results()
    assert results["r"].status == ToolStatus.OK and results["r"].content.startswith("Rolled [")
    assert results["bad"].status == ToolStatus.ERROR  # refused by its schema, not run


async def test_the_example_core_answers(make):
    h = await make(core=EchoCore())
    await h.start()
    await h.turn("hello there")
    [_, answer] = await h.transcript()
    assert answer.blocks[0].text.startswith("You said: ")
    assert answer.blocks[0].text.endswith("hello there")
    assert (await h.runtime.list_models())[0].id == "echo-1"


async def test_the_example_memory_keeps_a_conversation(make):
    memory = RamMemory()
    h = await make(memory=memory, core=ScriptedCore([Step(text="Noted."), Step(text="Tea.")]))
    await h.start()
    await h.turn("I drink green tea")
    await h.turn("what do I drink?")
    assert [m.role for m in await h.transcript()] == [Role.USER, Role.ASSISTANT] * 2
    assert len(h.core.requests[-1].messages) == 3  # the model saw the conversation so far
    [hit] = await memory.search("green tea")
    assert hit.conversation_id == h.conversation.id
    assert [c.id for c in await memory.list_conversations()] == [h.conversation.id]


async def test_the_example_memory_keeps_the_contracts_small_print():
    memory = RamMemory()
    info = await memory.create_conversation(title="Chat")
    first = Message("m1", Role.USER, (Text("hi"),), 0.0)
    await memory.append(info.id, [first])
    await memory.append(info.id, [first])  # the same again is a no-op
    assert await memory.history(info.id) == [first]
    with pytest.raises(ValueError):
        await memory.append(info.id, [Message("m1", Role.USER, (Text("other"),), 0.0)])
    with pytest.raises(KeyError):
        await memory.history("nope")
    assert (await memory.update_conversation(info.id, title="  ")).title is None


async def test_the_example_frontend_sends_lines_and_shows_answers(make):
    h = await make(core=ScriptedCore([Step(text="Hello!"), Step(text="Fine, thanks.")]))
    shown: list[str] = []

    async def lines() -> AsyncIterator[str]:
        for line in ("hi", "", "how are you?", "/quit", "never sent"):
            yield line

    await LineFrontend(lines, shown.append).run(h.runtime)
    assert shown == ["Hello!", "Fine, thanks."]
    assert len(h.core.requests) == 2
