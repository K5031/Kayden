"""Tool execution: backgrounding, timeouts, cancellation, abandon, ordering."""

from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest

from kayden.api import (
    BUILTIN_TOOLS,
    AgentState,
    Decision,
    OperationResult,
    PermissionPolicy,
    RuntimeStateError,
    ToolBackgrounded,
    ToolFinished,
    ToolOutput,
    ToolProgress,
    ToolSpec,
    ToolStatus,
)
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools


def finished(h: Harness, call_id: str) -> list[ToolFinished]:
    return [e for e in h.rec.of(ToolFinished) if e.result.call_id == call_id]


async def test_backgrounding(make: Any) -> None:
    tools = FakeTools()
    tools.slow("slow", 0.5)
    core = ScriptedCore([Step(calls=[call("slow", id="s")]), Step(text="meanwhile")])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=0.1)
    await h.start()
    await h.runtime.send("go")
    await eventually(lambda: h.runtime.snapshot().operations)
    assert [o.backgrounded for o in h.runtime.snapshot().operations] == [False]
    await h.rec.wait_state(AgentState.IDLE, count=2)
    assert [o.backgrounded for o in h.runtime.snapshot().operations] == [True]
    placeholder = (await h.results())["s"]
    assert placeholder.status == ToolStatus.BACKGROUNDED and "s" in placeholder.content
    assert h.rec.of(ToolBackgrounded) == [ToolBackgrounded("s")]
    assert finished(h, "s") == []  # not fired for the placeholder
    await h.rec.wait_for(lambda r: finished(h, "s"))
    assert finished(h, "s")[0].result.status == ToolStatus.OK
    assert finished(h, "s")[0].duration_s >= 0.45


async def test_stall_timeout(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("stuck", progress="started", stall_timeout=0.2)
    core = ScriptedCore([Step(calls=[call("stuck", id="s")]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=5)
    await h.start()
    await h.turn("go")
    result = (await h.results())["s"]
    assert result.status == ToolStatus.TIMED_OUT and "started" in result.content


async def test_stall_timeout_reset_by_progress(make: Any) -> None:
    tools = FakeTools()
    tools.slow("chatty", 0.6, steps=12, stall_timeout=0.2)
    core = ScriptedCore([Step(calls=[call("chatty", id="c")]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=5)
    await h.start()
    await h.turn("go")
    assert (await h.results())["c"].status == ToolStatus.OK


async def test_hard_timeout(make: Any) -> None:
    tools = FakeTools()
    tools.slow("long", 5, steps=100, timeout=0.2)
    core = ScriptedCore([Step(calls=[call("long", id="l")]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=5)
    await h.start()
    await h.turn("go")
    assert (await h.results())["l"].status == ToolStatus.TIMED_OUT


async def test_default_timeout(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("default")
    tools.hanging("unlimited", timeout=math.inf)
    core = ScriptedCore([Step(calls=[call("default", id="d"), call("unlimited", id="u")]),
                         Step(text="ok")])
    h: Harness = await make(core=core, tools=tools, default_tool_timeout_s=0.3)
    await h.start()
    await h.turn("go")
    await h.rec.wait_for(lambda r: finished(h, "d"))
    assert finished(h, "d")[0].result.status == ToolStatus.TIMED_OUT
    await asyncio.sleep(0.3)
    assert [o.call.id for o in h.runtime.snapshot().operations] == ["u"]


async def test_abandon_and_late_completion(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("stubborn", honor_cancel=False)
    core = ScriptedCore([Step(calls=[call("stubborn", id="s")])])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=5)
    await h.start()
    await h.runtime.send("go")
    await eventually(lambda: tools.started)
    await h.runtime.stop()
    await h.rec.wait_state(AgentState.STOPPED, timeout=2)
    result = (await h.results())["s"]
    assert result.status == ToolStatus.ABANDONED
    n_messages = len(await h.transcript())
    tools.release.set()
    await h.rec.wait_for(lambda r: len(finished(h, "s")) == 2)
    late = finished(h, "s")[1].result
    assert late.status == ToolStatus.OK and late.content == "stubborn late result"
    assert len(await h.transcript()) == n_messages  # never appended


async def test_thread_tool_progress(make: Any) -> None:
    tools = FakeTools()
    tools.threaded("crunch", 0.2)
    tools.threaded("endless", 30)
    core = ScriptedCore([Step(calls=[call("crunch", id="c")]),
                         Step(calls=[call("endless", id="e")])])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=5)
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_for(lambda r: any(e.call_id == "e" for e in r.of(ToolProgress)))
    assert any(e.call_id == "c" and e.text.startswith("tick") for e in h.rec.of(ToolProgress))
    assert (await h.results())["c"].status == ToolStatus.OK
    await h.runtime.stop()
    await h.rec.wait_state(AgentState.STOPPED)
    assert (await h.results())["e"].status == ToolStatus.CANCELLED


async def test_cancel_operation(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("bg", progress="working")
    core = ScriptedCore([Step(calls=[call("bg", id="bg")]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    await h.runtime.cancel_operation("bg")
    await h.rec.wait_for(lambda r: finished(h, "bg"))
    ops = [b for m in await h.transcript() for b in m.blocks if isinstance(b, OperationResult)]
    assert ops[0].status == ToolStatus.CANCELLED and "working" in ops[0].content
    with pytest.raises(RuntimeStateError):
        await h.runtime.cancel_operation("bg")


async def test_cancel_operation_abandons_after_grace(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("stubborn", honor_cancel=False)
    core = ScriptedCore([Step(calls=[call("stubborn", id="s")]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    await h.runtime.cancel_operation("s")
    await h.rec.wait_for(lambda r: finished(h, "s"))
    assert finished(h, "s")[0].result.status == ToolStatus.ABANDONED
    assert h.runtime.snapshot().operations == []


async def test_op_cancel(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("bg")
    core = ScriptedCore([
        Step(calls=[call("bg", id="bg")]),
        Step(calls=[call("op_cancel", {"call_id": "bg"}, id="c"),
                    call("op_cancel", {"call_id": "nope"}, id="n")]),
        Step(text="ok"),
    ])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    results = await h.results()
    assert results["c"].status == ToolStatus.OK and "cancel" in results["c"].content.lower()
    assert results["n"].status == ToolStatus.ERROR
    ops = [b for m in await h.transcript() for b in m.blocks if isinstance(b, OperationResult)]
    assert [(o.call_id, o.status) for o in ops] == [("bg", ToolStatus.CANCELLED)]


async def test_op_status_unknown(make: Any) -> None:
    core = ScriptedCore([Step(calls=[call("op_status", {"call_id": "nope"}, id="s")]),
                         Step(text="ok")])
    h: Harness = await make(core=core)
    await h.start()
    await h.turn("go")
    assert (await h.results())["s"].status == ToolStatus.ERROR


async def test_op_status_wait_clamped(make: Any, monkeypatch: Any) -> None:
    from kayden.runtime import orchestrator

    monkeypatch.setattr(orchestrator, "_OP_STATUS_MAX_WAIT_S", 0.2)
    tools = FakeTools()
    tools.slow("bg", 5)
    core = ScriptedCore([
        Step(calls=[call("bg", id="b1")]),
        Step(calls=[call("op_status", {"call_id": "b1", "wait_s": 3600}, id="s")]),
        Step(text="ok"),
    ])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await asyncio.wait_for(h.turn("go"), timeout=3)
    result = (await h.results())["s"]
    assert result.status == ToolStatus.OK and "Invalid arguments" not in result.content


async def run_turn(make: Any, ids: list[str], **config: Any) -> tuple[Harness, list[Any]]:
    """Run one turn calling ids in order; ids starting with "w" are non-read-only."""
    tools = FakeTools()
    for i in ids:
        tools.slow(i, 0.1, read_only=not i.startswith("w"))
    core = ScriptedCore([Step(calls=[call(i, id=i) for i in ids]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools, **{"initial_wait_s": 5, **config})
    approver = h.auto_approve(Decision.ALLOW_ONCE)
    await h.start()
    await h.turn("go")
    approver.cancel()
    assert list(await h.results()) == ids  # results in emission order
    return h, h.trace


def before(trace: list[Any], a: tuple[str, str], b: tuple[str, str]) -> bool:
    return trace.index(a) < trace.index(b)


async def test_barrier_write_then_read(make: Any) -> None:
    _, t = await run_turn(make, ["w", "r"])
    assert before(t, ("tool_end", "w"), ("tool_start", "r"))


async def test_barrier_read_then_write(make: Any) -> None:
    _, t = await run_turn(make, ["r", "w"])
    assert before(t, ("tool_end", "r"), ("tool_start", "w"))


async def test_barrier_reads_around_write(make: Any) -> None:
    _, t = await run_turn(make, ["r1", "r2", "w", "r3"])
    assert before(t, ("tool_start", "r2"), ("tool_end", "r1"))  # consecutive reads overlap
    assert before(t, ("tool_end", "r1"), ("tool_start", "w"))
    assert before(t, ("tool_end", "r2"), ("tool_start", "w"))
    assert before(t, ("tool_end", "w"), ("tool_start", "r3"))


async def test_backgrounded_call_releases_barrier(make: Any) -> None:
    h, t = await run_turn(make, ["w1", "w2"], initial_wait_s=0.03)
    await h.rec.wait_for(lambda r: len(r.of(ToolFinished)) == 2)
    assert before(t, ("tool_start", "w2"), ("tool_end", "w1"))
    assert (await h.results())["w1"].status == ToolStatus.BACKGROUNDED


async def test_reserved_names_dropped(make: Any) -> None:
    tools = FakeTools()

    async def fake_recall(c: Any, ctx: Any) -> ToolOutput:
        return ToolOutput("module recall")

    tools.add(ToolSpec("recall", "impostor", {"type": "object"}, True), fake_recall)
    tools.instant("fine")
    core = ScriptedCore([Step(calls=[call("recall", {"query": "x"}, id="r")]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    specs = {t.name: t for t in await h.runtime.list_tools()}
    assert specs["recall"] == BUILTIN_TOOLS["recall"] and "fine" in specs
    assert tools.started == []
    assert (await h.results())["r"].content != "module recall"


async def test_tool_context(make: Any) -> None:
    seen: dict[str, Any] = {}
    tools = FakeTools()

    async def probe(c: Any, ctx: Any) -> ToolOutput:
        seen["call_id"], seen["data_dir"] = ctx.call_id, ctx.data_dir
        ctx.progress("")  # silent heartbeat
        return ToolOutput("ok")

    tools.add(ToolSpec("probe", "probe", {"type": "object"}, True), probe)
    core = ScriptedCore([Step(calls=[call("probe", id="p")]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools, permissions=PermissionPolicy.ALLOW_READ_ONLY)
    await h.start()
    await h.turn("go")
    assert seen["call_id"] == "p" and seen["data_dir"].is_dir()
    assert not any(e.call_id == "p" for e in h.rec.of(ToolProgress))


async def test_foreground_cancel_operation(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("fg")
    core = ScriptedCore([Step(calls=[call("fg", id="fg")]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=5)
    await h.start()
    await h.runtime.send("go")
    await eventually(lambda: h.runtime.snapshot().operations)
    await h.runtime.cancel_operation("fg")
    await h.rec.wait_state(AgentState.IDLE, count=2)
    assert (await h.results())["fg"].status == ToolStatus.CANCELLED
    assert len(h.core.requests) == 2  # the cycle continues
