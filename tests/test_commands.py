"""Command x state table from RuntimeAPI: one test per cell, plus the footnotes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kadence.api import (
    AgentState,
    Budget,
    ConversationInfo,
    CoreError,
    Decision,
    MessageCommitted,
    MessageQueued,
    ModelSelected,
    PermissionPolicy,
    PermissionRequested,
    PermissionResolved,
    RuntimeStateError,
    StartOptions,
    Text,
    Usage,
)
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools

STATES = [
    AgentState.NOT_STARTED,
    AgentState.IDLE,
    AgentState.THINKING,
    AgentState.RUNNING_TOOLS,
    AgentState.AWAITING_PERMISSION,
    AgentState.STOPPING,
    AgentState.STOPPED,
    AgentState.FAILED,
]

OK, RAISE, QUEUE, NOOP = "ok", "raise", "queue", "noop"
I, T, R, A, S, X, F = (AgentState.IDLE, AgentState.THINKING, AgentState.RUNNING_TOOLS,
                       AgentState.AWAITING_PERMISSION, AgentState.STOPPING, AgentState.STOPPED,
                       AgentState.FAILED)
N = AgentState.NOT_STARTED

TABLE: dict[str, dict[Any, str]] = {
    "start": {N: OK, I: OK, T: RAISE, R: RAISE, A: RAISE, S: RAISE, X: OK, F: OK},
    "send": {N: RAISE, I: OK, T: QUEUE, R: QUEUE, A: QUEUE, S: QUEUE, X: OK, F: OK},
    "activate": {N: RAISE, I: OK, T: RAISE, R: RAISE, A: RAISE, S: RAISE, X: OK, F: OK},
    "stop": {N: NOOP, I: NOOP, T: OK, R: OK, A: OK, S: NOOP, X: NOOP, F: NOOP},
    "select_model": {N: OK, I: OK, T: OK, R: OK, A: OK, S: OK, X: OK, F: OK},
    "resolve_permission": {N: RAISE, I: RAISE, T: RAISE, R: RAISE, A: OK, S: RAISE, X: RAISE,
                           F: RAISE},
}


async def enter(make: Any, state: Any) -> Harness:
    tools = FakeTools()
    tools.hanging("hang")
    tools.hanging("stubborn", honor_cancel=False)
    tools.instant("write", read_only=False)
    steps = {
        T: Step(text="...", hang=True),
        R: Step(calls=[call("hang")]),
        A: Step(calls=[call("write")]),
        S: Step(calls=[call("stubborn")]),
        X: Step(text="...", hang=True),
        F: Step(error=CoreError("boom")),
    }
    core = ScriptedCore(default=steps.get(state, Step(text="hi")))
    h: Harness = await make(core=core, tools=tools, initial_wait_s=30, cancel_grace_s=10,
                            permissions=PermissionPolicy.ASK if state == A
                            else PermissionPolicy.ALLOW_READ_ONLY)
    if state == N:
        return h
    await h.start()
    if state == I:
        await h.rec.wait_state(I)
        return h
    await h.runtime.send("go")
    target = {X: T, S: R}.get(state, state)
    await h.rec.wait_state(target)
    if target == T:  # the cycle has started, not merely been requested
        await eventually(lambda: core.requests)
    if target == R:  # the tool coroutine is actually running
        await eventually(lambda: tools.started)
    if state in (X, S):
        await h.runtime.stop()
        await h.rec.wait_state(state)
    assert h.runtime.snapshot().state == state
    return h


async def run_command(h: Harness, command: str) -> None:
    rt = h.runtime
    match command:
        case "start":
            info = await rt.start(StartOptions())
            assert isinstance(info, ConversationInfo)
        case "send":
            await rt.send("hello")
        case "activate":
            await rt.activate()
        case "stop":
            await rt.stop()
        case "select_model":
            await rt.select_model("fake-2")
        case "resolve_permission":
            requests = h.rec.of(PermissionRequested)
            await rt.resolve_permission(requests[-1].request.id if requests else "bogus",
                                        Decision.ALLOW_ONCE)


@pytest.mark.parametrize("state", STATES, ids=str)
@pytest.mark.parametrize("command", list(TABLE))
async def test_command_state_cell(make: Any, command: str, state: Any) -> None:
    expected = TABLE[command][state]
    h = await enter(make, state)
    seq = h.runtime.snapshot().seq
    if seq:  # let the recorder catch up
        await h.rec.wait_for(lambda r: r.envelopes and r.envelopes[-1].seq >= seq)
    states_before = list(h.rec.states)
    if expected == RAISE:
        with pytest.raises(RuntimeStateError):
            await run_command(h, command)
        return
    await run_command(h, command)
    await asyncio.sleep(0.05)
    new = [env.event for env in h.rec.envelopes if env.seq > seq]
    key = (command, expected)
    if key == ("send", QUEUE):
        assert any(isinstance(e, MessageQueued) and e.text == "hello" for e in new)
        assert not any(isinstance(e, MessageCommitted) and e.message.blocks == (Text("hello"),)
                       for e in new)
    elif key == ("send", OK):
        await h.rec.wait_for(lambda r: any(
            isinstance(e, MessageCommitted) and e.message.blocks == (Text("hello"),)
            for e in r.events))
        await h.rec.wait_for(lambda r: AgentState.THINKING in r.states[len(states_before):])
    elif key == ("activate", OK):
        await h.rec.wait_for(lambda r: AgentState.THINKING in r.states[len(states_before):])
    elif key == ("stop", OK):
        await h.rec.wait_for(lambda r: AgentState.STOPPING in r.states[len(states_before):])
    elif key == ("stop", NOOP):
        assert h.rec.states == states_before, [e for e in new]
        assert h.runtime.snapshot().state == state
    elif key == ("select_model", OK):
        assert ModelSelected("fake-2") in new
        assert h.runtime.snapshot().model == "fake-2"
    elif key == ("resolve_permission", OK):
        assert any(isinstance(e, PermissionResolved) for e in new)
    elif key == ("start", OK):
        assert h.runtime.snapshot().conversation is not None
    else:
        raise AssertionError(f"unchecked cell {key}")


# ── Footnotes ──


async def test_start_refused_with_outstanding_ops(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("bg")
    core = ScriptedCore([Step(calls=[call("bg", id="bg")]), Step(text="waiting"),
                         Step(text="it was cancelled")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    assert h.runtime.snapshot().operations
    with pytest.raises(RuntimeStateError):
        await h.start()
    first = h.conversation
    await h.runtime.cancel_operation("bg")
    # The agent is woken by the outcome, answers, and is idle again.
    await eventually(lambda: len(core.requests) == 3
                     and h.runtime.snapshot().state == AgentState.IDLE)
    other = await h.start()
    assert first is not None and other.id != first.id


async def test_activate_in_idle(make: Any) -> None:
    h: Harness = await make()
    await h.start()
    await h.rec.wait_state(AgentState.IDLE)
    await h.runtime.activate()
    await h.rec.wait_state(AgentState.THINKING)


async def test_failed_budget_blocks_send_and_activate(make: Any) -> None:
    core = ScriptedCore(default=Step(text="x", usage=Usage(5000, 100)))
    # Above the request's own estimate, below what the first turn reports.
    h: Harness = await make(core=core, budget=Budget(max_tokens=5050))
    await h.start()
    await h.turn("spend")
    await h.runtime.send("more")
    await h.rec.wait_state(AgentState.FAILED)
    with pytest.raises(RuntimeStateError):
        await h.runtime.send("again")
    with pytest.raises(RuntimeStateError):
        await h.runtime.activate()
    await h.start()  # start is still allowed in FAILED


async def test_failed_model_allows_activate(make: Any) -> None:
    core = ScriptedCore([Step(error=CoreError("boom")), Step(text="better now")])
    h: Harness = await make(core=core)
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_state(AgentState.FAILED)
    await h.runtime.activate()
    await h.rec.wait_state(AgentState.IDLE, count=2)
    assert len(core.requests) == 2


async def test_resolve_permission_unknown_and_twice(make: Any) -> None:
    h = await enter(make, A)
    request = h.rec.of(PermissionRequested)[-1].request
    with pytest.raises(RuntimeStateError):
        await h.runtime.resolve_permission("unknown", Decision.ALLOW_ONCE)
    await h.runtime.resolve_permission(request.id, Decision.DENY)
    with pytest.raises(RuntimeStateError):
        await h.runtime.resolve_permission(request.id, Decision.ALLOW_ONCE)


async def test_cancel_operation_requires_running_call(make: Any) -> None:
    h = await enter(make, R)
    running = h.runtime.snapshot().operations[0].call.id
    with pytest.raises(RuntimeStateError):
        await h.runtime.cancel_operation("not-a-call")
    await h.runtime.cancel_operation(running)
    await h.rec.wait_for(lambda r: any(getattr(e, "result", None) is not None
                                       and e.result.call_id == running for e in r.events))
    with pytest.raises(RuntimeStateError):
        await h.runtime.cancel_operation(running)
