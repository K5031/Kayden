"""Working freely in the work folder: confined calls run unasked, the rest prompt."""

from __future__ import annotations

from typing import Any

from kadence.api import AgentState, Decision, PermissionPolicy, ToolCall, Tools, ToolStatus
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools


def two_tools() -> FakeTools:
    tools = FakeTools()
    tools.instant("inside", read_only=False)
    tools.instant("outside")  # read-only, but not confined
    tools.confined_names = {"inside"}
    return tools


def core() -> ScriptedCore:
    return ScriptedCore([Step(calls=[call("inside", id="i"), call("outside", id="o")]),
                         Step(text="done")])


async def prompts(h: Harness) -> list[Any]:
    await h.start()
    await h.runtime.send("go")
    await eventually(lambda: h.runtime.snapshot().state == AgentState.AWAITING_PERMISSION)
    return list(h.runtime.snapshot().pending_permissions)


async def test_confined_calls_run_and_the_rest_ask(make: Any) -> None:
    tools = two_tools()
    h: Harness = await make(core=core(), tools=tools,
                            permissions=PermissionPolicy.ALLOW_WORKSPACE)
    [request] = await prompts(h)
    assert request.call.id == "o" and not request.confined  # reading outside asks too
    assert tools.started == ["i"]
    await h.runtime.resolve_permission(request.id, Decision.ALLOW_ONCE)
    await eventually(lambda: h.runtime.snapshot().state == AgentState.IDLE)
    assert {r.status for r in (await h.results()).values()} == {ToolStatus.OK}


async def test_a_prompt_says_whether_the_call_is_confined(make: Any) -> None:
    h: Harness = await make(core=core(), tools=two_tools(), permissions=PermissionPolicy.ASK)
    [request] = await prompts(h)
    assert request.call.id == "i" and request.confined


async def test_tools_that_cannot_tell_ask_for_everything(make: Any) -> None:
    tools = two_tools()

    async def broken(call: ToolCall) -> bool:
        raise RuntimeError("boom")

    tools.confined = broken  # type: ignore[method-assign]
    h: Harness = await make(core=core(), tools=tools,
                            permissions=PermissionPolicy.ALLOW_WORKSPACE)
    [request] = await prompts(h)
    assert request.call.id == "i" and not request.confined
    assert await Tools.confined(tools, ToolCall("x", "inside", {})) is False  # the default


def test_strictness_order() -> None:
    assert [p.value for p in PermissionPolicy] == [
        "deny", "ask", "allow_read_only", "allow_workspace", "auto"]
    workspace, read_only = PermissionPolicy.ALLOW_WORKSPACE, PermissionPolicy.ALLOW_READ_ONLY
    assert workspace.clamp(read_only) is read_only and read_only.clamp(workspace) is read_only


async def test_a_grant_for_a_confined_call_does_not_cover_one_that_reaches_outside(
        make: Any) -> None:
    tools = FakeTools()
    tools.instant("write", read_only=False)

    async def by_argument(call: ToolCall) -> bool:
        return bool(call.arguments.get("inside"))

    tools.confined = by_argument  # type: ignore[method-assign]
    script = ScriptedCore([
        Step(calls=[call("write", {"inside": True}, id="a")]),
        Step(calls=[call("write", {"inside": True}, id="b"),
                    call("write", {"inside": False}, id="c")]),
        Step(calls=[call("write", {"inside": False}, id="d")]),
        Step(text="done"),
    ])
    h: Harness = await make(core=script, tools=tools, permissions=PermissionPolicy.ASK)
    [first] = await prompts(h)
    assert first.call.id == "a"
    await h.runtime.resolve_permission(first.id, Decision.ALLOW_CONVERSATION)
    await eventually(lambda: [p.call.id for p in h.runtime.snapshot().pending_permissions]
                     == ["c"])
    assert tools.started == ["a", "b"]  # b ran on the grant; c reaches outside and asks
    [outside] = h.runtime.snapshot().pending_permissions
    await h.runtime.resolve_permission(outside.id, Decision.ALLOW_CONVERSATION)
    await eventually(lambda: h.runtime.snapshot().state == AgentState.IDLE)
    assert tools.started == ["a", "b", "c", "d"]  # d ran on the wider grant
