"""Every tool call can carry a plain-language description the model writes; the runtime
offers it on every tool and takes it out before the tool sees the call."""

from __future__ import annotations

from typing import Any

from kayden.api import DESCRIPTION_ARG, PermissionPolicy, ToolSpec, ToolStarted
from tests.conftest import Harness
from tests.fakes.core import ScriptedCore, Step, call

STRICT = {"type": "object", "properties": {"path": {"type": "string"}},
          "required": ["path"], "additionalProperties": False}


async def harness(make: Any, *steps: Step) -> Harness:
    h = await make(core=ScriptedCore([*steps, Step(text="done")]),
                   permissions=PermissionPolicy.ALLOW_WORKSPACE)
    h.tools.instant("read", schema=STRICT)
    h.tools.confined_names = {"read", "own"}
    return h


async def test_every_offered_tool_takes_a_description(make: Any) -> None:
    h = await harness(make)
    await h.start()
    for spec in await h.runtime.list_tools():
        prop = spec.parameters["properties"][DESCRIPTION_ARG]
        assert prop["type"] == "string" and "plain" in prop["description"], spec.name
        assert DESCRIPTION_ARG not in spec.parameters.get("required", [])
    assert h.tools.specs["read"].parameters == STRICT  # the module's own spec is untouched


async def test_the_tool_never_sees_the_description(make: Any) -> None:
    h = await harness(make, Step(calls=[call("read", {"path": "a.txt",
                                                      DESCRIPTION_ARG: "Read your notes"},
                                             id="r")]))
    await h.start()
    await h.turn("go")
    assert [c.arguments for c in h.tools.received] == [{"path": "a.txt"}] * 2  # confined, run
    [started] = h.rec.of(ToolStarted)
    assert started.call.arguments[DESCRIPTION_ARG] == "Read your notes"  # frontends show it
    [stored] = [b for m in await h.transcript() for b in m.blocks
                if getattr(b, "id", None) == "r"]
    assert stored.arguments[DESCRIPTION_ARG] == "Read your notes"  # and history keeps it


async def test_a_call_without_a_description_runs_as_before(make: Any) -> None:
    h = await harness(make, Step(calls=[call("read", {"path": "a.txt"}, id="r")]))
    await h.start()
    await h.turn("go")
    assert (await h.results())["r"].content.startswith("done")


async def test_a_tool_with_its_own_description_argument_keeps_it(make: Any) -> None:
    h = await harness(make, Step(calls=[call("own", {"step_summary": "a ticket text"},
                                             id="o")]))
    own = {"type": "object", "properties": {"step_summary": {"type": "string"}}}
    h.tools.add(ToolSpec("own", "files a ticket", own), h.tools.fns["read"])
    await h.start()
    await h.turn("go")
    assert h.tools.received[-1].arguments == {"step_summary": "a ticket text"}
