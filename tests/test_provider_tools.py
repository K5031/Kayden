"""Tools the provider runs itself (Core.list_tools): offered first among module tools,
never run or gated by the runtime, recorded as ServerToolUse blocks, charged when priced."""

from __future__ import annotations

from typing import Any

from kayden.api import (
    PermissionPolicy,
    ServerToolUse,
    ToolSpec,
    ToolStarted,
    Usage,
)
from kayden.memory.sqlite import codec
from tests.conftest import Harness
from tests.fakes.core import ScriptedCore, Step
from tests.fakes.tools import FakeTools

SEARCH = ToolSpec("web_search", "Search the web (run by the provider).",
                  {"type": "object", "properties": {"query": {"type": "string"}}})
USE = ServerToolUse("srv_1", "web_search", {"query": "worlds 2026 final"},
                    "Searched the web for “worlds 2026 final”",
                    "2026 League of Legends World Championship\nhttps://en.wikipedia.org/x",
                    state={"provider": "test", "blocks": []})


async def harness(make: Any, *steps: Step, **config: Any) -> Harness:
    core = ScriptedCore([*steps, Step(text="done")])
    core.tools = [SEARCH]
    tools = FakeTools()
    tools.instant("web_search", "Kayden's own search")
    tools.instant("web_fetch", "a page")
    return await make(core=core, tools=tools, **config)


async def test_the_providers_tool_is_offered_in_place_of_a_modules(make: Any) -> None:
    h = await harness(make, Step(text="Here.", server=[USE]))
    await h.start()
    await h.turn("who plays the final?")
    offered = {t.name: t for t in h.core.requests[0].tools}
    assert offered["web_search"].description == SEARCH.description and "web_fetch" in offered


async def test_its_use_is_recorded_not_run(make: Any) -> None:
    h = await harness(make, Step(text="Gen.G and BLG.", server=[USE]),
                      permissions=PermissionPolicy.DENY)
    await h.start()
    await h.turn("who plays the final?")
    transcript = await h.transcript()
    [answer] = [m for m in transcript if USE in m.blocks]
    assert answer.blocks[-1].text == "Gen.G and BLG."
    assert not h.tools.started and not h.rec.of(ToolStarted)  # nothing ran here, ungated
    assert h.runtime.snapshot().usage.tool_calls == 0  # nor counted as a tool call


async def test_what_the_provider_charges_beyond_tokens_is_spent(make: Any) -> None:
    h = await harness(make, Step(text="ok", server=[USE],
                                 usage=Usage(100, 10, extra_cost_usd=0.01)))
    await h.start()
    await h.turn("search")
    with_search = h.runtime.snapshot().usage.usage.cost_usd
    plain = await harness(make, Step(text="ok", usage=Usage(100, 10)))
    await plain.start()
    await plain.turn("search")
    assert with_search == plain.runtime.snapshot().usage.usage.cost_usd + 0.01


def test_a_use_is_stored_and_searchable():
    raw = codec.encode_blocks([USE])
    assert codec.decode_blocks(raw) == (USE,)
    text = codec.searchable_text(raw)
    assert "worlds 2026 final" in text and "World Championship" in text
    assert "srv_1" not in text and "provider" not in text
