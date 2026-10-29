"""Runtime guarantees from the api.py module docstring."""

from __future__ import annotations

import asyncio
from typing import Any

from kayden.api import (
    Activation,
    AgentState,
    Budget,
    BudgetExhausted,
    Completion,
    Failure,
    FailureKind,
    Message,
    MessageCommitted,
    MessageQueued,
    Mode,
    OperationResult,
    Role,
    StopReason,
    Text,
    ToolCall,
    ToolFinished,
    ToolResult,
    ToolStarted,
    ToolStatus,
    Usage,
)
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.memory import Conversation, MemoryStore
from tests.fakes.tools import FakeTools


def appends(h: Harness) -> list[tuple[Any, ...]]:
    return [t for t in h.trace if t[0] == "append"]


# ── Durability order ──


async def test_assistant_appended_before_tool_start(make: Any) -> None:
    tools = FakeTools()
    tools.instant("t")
    core = ScriptedCore([Step(calls=[call("t", id="c1"), call("t", id="c2")]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    assistant = next(m for m in await h.transcript() if m.role == Role.ASSISTANT)
    i_append = h.trace.index(next(t for t in appends(h) if t[2] == assistant.id))
    for cid in ("c1", "c2"):
        assert i_append < h.trace.index(("tool_start", cid))


async def test_tool_result_appended_before_tool_finished(make: Any) -> None:
    tools = FakeTools()
    tools.instant("t")
    tools.slow("bg", 0.3)
    core = ScriptedCore([Step(calls=[call("t", id="c1"), call("bg", id="c2")]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    await h.rec.wait_for(lambda r: len(r.of(ToolFinished)) == 2)
    transcript = await h.transcript()
    seq_at_append = {t[2]: t[3] for t in appends(h)}
    for env in h.rec.envelopes:
        if isinstance(env.event, ToolFinished):
            cid = env.event.result.call_id
            holder = next(
                m for m in transcript
                if any(getattr(b, "call_id", None) == cid and b.status == env.event.result.status
                       for b in m.blocks)
            )
            assert seq_at_append[holder.id] < env.seq


async def test_user_message_appended_before_committed(make: Any) -> None:
    h: Harness = await make()
    await h.start()
    await h.turn("hello")
    user = next(m for m in await h.transcript() if m.role == Role.USER)
    committed = next(env for env in h.rec.envelopes if isinstance(env.event, MessageCommitted)
                     and env.event.message.id == user.id)
    assert next(t for t in appends(h) if t[2] == user.id)[3] < committed.seq


async def test_append_failure_stops_further_work(make: Any) -> None:
    tools = FakeTools()
    tools.instant("t")
    core = ScriptedCore([Step(calls=[call("t", id="c1")], delay=0.1), Step(text="recovered")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.runtime.send("go")
    await eventually(lambda: len(core.requests) == 1)
    h.memory.fail_append = 1  # the assistant message
    await h.rec.wait_state(AgentState.FAILED)
    failure = h.rec.of(Failure)[-1]
    assert failure.kind == FailureKind.INTERNAL
    assert tools.started == []
    await asyncio.sleep(0.1)
    assert len(core.requests) == 1
    await h.runtime.activate()
    await h.rec.wait_state(AgentState.IDLE, count=2)
    transcript = await h.transcript()
    # The replayed transcript ends on the TOOL message, so activation adds a notice.
    assert [m.role for m in transcript] == [Role.USER, Role.ASSISTANT, Role.TOOL, Role.USER,
                                            Role.ASSISTANT]
    assert transcript[2].blocks[0].status == ToolStatus.ERROR
    assert tools.started == []


# ── Serialization of mutating Memory calls ──


async def test_maintain_ordering_and_serialization(make: Any) -> None:
    tools = FakeTools()
    tools.instant("t")
    tools.slow("bg", 0.2)
    core = ScriptedCore([
        Step(calls=[call("t"), call("bg")]),
        Step(calls=[call("t")]),
        Step(text="done"),
    ])
    h: Harness = await make(core=core, tools=tools, memory_kw={"maintain_delay": 0.02})
    await h.start()
    await h.turn("go")
    names = [t[0] for t in h.trace if not t[0].startswith("tool_")]  # bg may end anywhere
    assert h.memory.max_mutating == 1
    # Every inference is preceded by maintain then assemble; nothing mutates during inference.
    for i, name in enumerate(names):
        if name == "infer_start":
            assert names[i - 1] == "assemble" and names[i - 2] == "maintain_end"
            end = names.index("infer_end", i)
            assert not {"append", "maintain_start"} & set(names[i:end])
    # maintain runs after the turn's results are appended
    tool_appends = [i for i, t in enumerate(h.trace) if t[0] == "append" and t[1] == Role.TOOL]
    for i in tool_appends:
        assert "maintain_start" in [t[0] for t in h.trace[i:]]


async def test_stop_cancels_maintain(make: Any) -> None:
    h: Harness = await make(memory_kw={"maintain_delay": 30})
    await h.start()
    await h.runtime.send("go")
    await eventually(lambda: ("maintain_start",) in h.trace)
    await h.runtime.stop()
    await h.rec.wait_state(AgentState.STOPPED, timeout=2)
    assert ("maintain_end",) in h.trace
    assert h.core.requests == []


async def test_budget_rechecked_after_maintain(make: Any) -> None:
    core = ScriptedCore(default=Step(text="summary", usage=Usage(1000, 100)))
    h: Harness = await make(core=core, budget=Budget(max_tokens=1000))

    async def maintain(complete: Completion) -> bool:
        await complete("Summarize.", [Message("m", Role.USER, (Text("x"),), 0.0)],
                       max_tokens=50, light=True)
        return True

    h.memory.maintain_override = maintain
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_state(AgentState.FAILED)
    assert len(core.requests) == 1 and not core.requests[0].tools
    assert h.rec.of(Failure)[-1].kind == FailureKind.BUDGET


async def test_completion_budget_exhausted(make: Any) -> None:
    core = ScriptedCore(default=Step(text="ok", usage=Usage(1000, 100)))
    h: Harness = await make(core=core, budget=Budget(max_tokens=1000))
    caught: list[BaseException] = []

    async def maintain(complete: Completion) -> bool:
        if len(core.requests) == 0:
            return False
        try:
            await complete("Summarize.", [], max_tokens=50)
        except BudgetExhausted as e:
            caught.append(e)
            raise
        return True

    h.memory.maintain_override = maintain
    await h.start()
    await h.turn("first")  # spends 1100 tokens
    await h.runtime.send("second")
    await h.rec.wait_state(AgentState.FAILED)
    assert len(caught) == 1 and len(core.requests) == 1
    assert h.rec.of(Failure)[-1].kind == FailureKind.BUDGET


# ── Resume repair ──


async def test_resume_repair(make: Any) -> None:
    store = MemoryStore()
    seed: Harness = await make(store=store)
    info = await seed.memory.create_conversation(Mode.CONTINUOUS)
    await seed.memory.append(info.id, [
        Message("u1", Role.USER, (Text("start"),), 1.0),
        Message("a1", Role.ASSISTANT, (ToolCall("bg", "slow", {}),), 2.0),
        Message("t1", Role.TOOL, (ToolResult("bg", ToolStatus.BACKGROUNDED, "running"),), 3.0),
        Message("a2", Role.ASSISTANT, (ToolCall("x", "write", {}), ToolCall("y", "read", {})), 4.0),
    ])
    await seed.close()

    h: Harness = await make(store=store, data_dir=seed.data_dir,
                            core=ScriptedCore(default=Step(text="I see")))
    await h.start(Mode.CONTINUOUS, Activation.AGENT_CONTROLLED)
    assert h.runtime.snapshot().state == AgentState.STOPPED
    transcript = await h.transcript()
    assert [m.role for m in transcript[4:]] == [Role.TOOL, Role.USER]
    assert [(b.call_id, b.status) for b in transcript[4].blocks] == [
        ("x", ToolStatus.INTERRUPTED), ("y", ToolStatus.INTERRUPTED)]
    assert transcript[5].blocks == (
        OperationResult("bg", ToolStatus.INTERRUPTED, transcript[5].blocks[0].content),)
    await asyncio.sleep(0.1)
    assert h.core.requests == []
    await h.runtime.activate()
    await eventually(lambda: len(h.core.requests) == 1)
    seen = [m.id for m in h.core.requests[0].messages]
    assert transcript[4].id in seen and transcript[5].id in seen
    await h.close()

    # Repair is idempotent: a second restart adds nothing.
    h2: Harness = await make(store=store, data_dir=seed.data_dir)
    await h2.start(Mode.CONTINUOUS)
    conv: Conversation = store.conversations[info.id]
    assert sum(1 for m in conv.messages
               for b in m.blocks if getattr(b, "status", None) == ToolStatus.INTERRUPTED) == 3


# ── Validation ──


async def test_tool_call_validation(make: Any) -> None:
    tools = FakeTools()
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    tools.instant("echo", schema=schema)
    core = ScriptedCore([
        Step(calls=[
            call("nope", id="unknown"),
            call("echo", {"n": "x"}, id="badschema"),
            ToolCall("unparsed", "echo", {}, raw_arguments='{"n": '),
            call("idle", id="idle"),  # not offered in Session Mode
            call("echo", {"n": 1}, id="good"),
        ]),
        Step(text="ok"),
    ])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    results = await h.results()
    for cid in ("unknown", "badschema", "unparsed", "idle"):
        assert results[cid].status == ToolStatus.ERROR, cid
    assert results["good"].status == ToolStatus.OK
    assert tools.started == ["good"]


# ── Wrapping ──


async def test_tool_exception_wrapped(make: Any) -> None:
    tools = FakeTools()
    tools.failing("boom")
    tools.failing("soft", raises=False)
    core = ScriptedCore([Step(calls=[call("boom", id="b"), call("soft", id="s")]),
                         Step(text="handled")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    results = await h.results()
    assert results["b"].status == ToolStatus.ERROR and "exploded" in results["b"].content
    assert results["s"].status == ToolStatus.ERROR and results["s"].content == "soft failed"
    assert h.runtime.snapshot().state == AgentState.IDLE


async def test_core_unexpected_exception_is_permanent(make: Any) -> None:
    core = ScriptedCore(default=Step(text="x", error=ValueError("bug in core")))
    h: Harness = await make(core=core)
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_state(AgentState.FAILED)
    assert len(core.requests) == 1
    assert h.rec.of(Failure)[-1].kind == FailureKind.MODEL


async def test_module_exceptions_become_failures(make: Any) -> None:
    tools = FakeTools()
    core = ScriptedCore([
        Step(calls=[call("recall", {"query": "x"}, id="r")]),
        Step(text="carried on"),
    ])
    h: Harness = await make(core=core, tools=tools)
    h.memory.search_error = RuntimeError("index corrupt")
    await h.start()
    await h.turn("go")
    result = (await h.results())["r"]
    assert result.status == ToolStatus.ERROR and "index corrupt" in result.content

    tools.list_error = RuntimeError("tool server down")
    core.add(Step(text="still here"))
    await h.turn("again")
    assert any(f.kind == FailureKind.INTERNAL and "tool server down" in f.message
               for f in h.rec.of(Failure))
    assert {t.name for t in core.requests[-1].tools} >= {"recall", "op_status"}


# ── One cycle at a time ──


async def test_single_cycle_at_a_time(make: Any) -> None:
    core = ScriptedCore(default=Step(text="slow reply", chunks=5, delay=0.05))
    h: Harness = await make(core=core)
    await h.start()
    await h.runtime.send("one")
    await eventually(lambda: core.requests)
    await asyncio.gather(h.runtime.send("two"), h.runtime.send("three"))
    assert len(h.rec.of(MessageQueued)) == 2
    await h.rec.wait_state(AgentState.IDLE, count=2)
    assert core.max_active == 1
    assert len([r for r in core.requests if r.tools]) == 2
    texts = [b.text for m in core.requests[1].messages if m.role == Role.USER
             for b in m.blocks if isinstance(b, Text)]
    assert texts == ["one", "two", "three"]


# ── Turn boundary ──


async def test_turn_boundary_order(make: Any) -> None:
    tools = FakeTools()
    tools.gated("bg")
    tools.gated("fg")
    core = ScriptedCore([
        Step(calls=[call("bg", id="bg")]),
        Step(calls=[call("fg", id="fg")]),
        Step(text="end"),
    ])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=1.0)
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_for(lambda r: any(e.call.id == "fg" for e in r.of(ToolStarted)))
    tools.gates["bg"].set()
    await eventually(lambda: "bg" in tools.finished)
    await h.runtime.send("queued note")
    tools.gates["fg"].set()
    await h.rec.wait_state(AgentState.IDLE, count=2)
    transcript = await h.transcript()
    i = next(i for i, m in enumerate(transcript)
             if m.role == Role.TOOL and m.blocks[0].call_id == "fg")
    tool_msg, ops_msg, queued = transcript[i : i + 3]
    assert tool_msg.blocks[0].status == ToolStatus.OK
    assert ops_msg.role == Role.USER and ops_msg.blocks == (
        OperationResult("bg", ToolStatus.OK, "bg done"),)
    assert queued.role == Role.USER and queued.blocks == (Text("queued note"),)
    assert queued.id == h.rec.of(MessageQueued)[0].message_id


# ── Budget reservation ──


async def test_tool_call_budget_reserved_atomically(make: Any) -> None:
    tools = FakeTools()
    for n in ("r1", "r2", "r3"):
        tools.slow(n, 0.05)
    core = ScriptedCore([Step(calls=[call(n, id=n) for n in ("r1", "r2", "r3")])])
    h: Harness = await make(core=core, tools=tools, budget=Budget(max_tool_calls=2))
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_state(AgentState.FAILED)
    statuses = sorted(r.status for r in (await h.results()).values())
    assert statuses == sorted([ToolStatus.OK, ToolStatus.OK, ToolStatus.REFUSED])
    assert len(tools.started) == 2
    assert h.runtime.snapshot().usage.tool_calls == 2


# ── Message conventions ──


async def test_one_tool_message_per_turn(make: Any) -> None:
    tools = FakeTools()
    tools.instant("a")
    tools.slow("b", 0.3)
    core = ScriptedCore([
        Step(calls=[call("a"), call("b"), call("nope")]),
        Step(calls=[call("a")], stop=StopReason.TOOL_USE),
        Step(text="done"),
    ])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    transcript = await h.transcript()
    for i, m in enumerate(transcript):
        calls = [b.id for b in m.blocks if isinstance(b, ToolCall)]
        if m.role == Role.ASSISTANT and calls:
            nxt = transcript[i + 1]
            assert nxt.role == Role.TOOL
            assert [b.call_id for b in nxt.blocks] == calls
            assert all(isinstance(b, ToolResult) for b in nxt.blocks)
        if m.role == Role.TOOL:
            assert transcript[i - 1].role == Role.ASSISTANT
    assert len({m.id for m in transcript}) == len(transcript)
