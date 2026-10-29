"""Budget semantics (SPEC section 9)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kayden.api import (
    AgentState,
    Budget,
    Decision,
    Failure,
    FailureKind,
    Mode,
    PermissionPolicy,
    PermissionRequested,
    ToolFinished,
    ToolStatus,
    TransientCoreError,
    Usage,
)
from tests.conftest import Harness
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools


async def test_tokens_count_input_and_output(make: Any) -> None:
    core = ScriptedCore(default=Step(text="ok", usage=Usage(300, 40, cached_tokens=200)))
    h: Harness = await make(core=core, budget=Budget(max_tokens=10_000))
    await h.start()
    await h.turn("a")
    await h.turn("b")
    usage = h.runtime.snapshot().usage
    assert usage.usage.input_tokens == 600 and usage.usage.output_tokens == 80
    assert usage.usage.cached_tokens == 400
    assert usage.budget_remaining["max_tokens"] == 10_000 - 680  # cached not added again


async def test_cost_reported(make: Any) -> None:
    core = ScriptedCore(default=Step(text="ok", usage=Usage(1000, 100, cost_usd=0.5)))
    h: Harness = await make(core=core, budget=Budget(max_cost_usd=10))
    await h.start()
    await h.turn("a")
    assert h.runtime.snapshot().usage.usage.cost_usd == pytest.approx(0.5)
    assert h.runtime.snapshot().usage.budget_remaining["max_cost_usd"] == pytest.approx(9.5)


async def test_cost_from_prices(make: Any) -> None:
    usage = Usage(1_000_000, 500_000, cached_tokens=200_000)
    h: Harness = await make(core=ScriptedCore(default=Step(text="ok", usage=usage)))
    await h.start()
    await h.turn("a")
    # fake-1: input 1.0, cached 0.5, output 2.0 USD per Mtok
    assert h.runtime.snapshot().usage.usage.cost_usd == pytest.approx(0.8 + 0.1 + 1.0)


async def test_cost_budget_requires_prices(make: Any) -> None:
    h: Harness = await make(budget=Budget(max_cost_usd=1.0))
    with pytest.raises(ValueError):
        await h.start(model="fake-2")
    await h.start(model="fake-1")
    with pytest.raises(ValueError):
        await h.runtime.select_model("fake-2")


async def test_duration_counts_running_time(make: Any) -> None:
    tools = FakeTools()
    tools.slow("fg", 0.2)
    tools.slow("bg", 0.6)
    core = ScriptedCore([Step(calls=[call("fg")]), Step(calls=[call("bg", id="bg")]),
                         Step(text="ok")])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=0.3,
                            budget=Budget(max_duration_s=100))
    await h.start()
    await h.turn("go")
    await h.rec.wait_for(lambda r: any(e.result.call_id == "bg" for e in r.of(ToolFinished)))
    elapsed = h.runtime.snapshot().usage.elapsed_s
    assert 0.75 <= elapsed < 1.5  # fg 0.2 + bg 0.6 (background included), not serial idle time
    await asyncio.sleep(0.3)
    assert h.runtime.snapshot().usage.elapsed_s == pytest.approx(elapsed, abs=0.01)


async def test_duration_excludes_permission_wait_and_backoff(make: Any) -> None:
    tools = FakeTools()
    tools.instant("write", read_only=False)
    core = ScriptedCore([
        Step(error=TransientCoreError("busy", retry_after=0.4)),
        Step(calls=[call("write")]),
        Step(text="ok"),
    ])
    h: Harness = await make(core=core, tools=tools, permissions=PermissionPolicy.ASK,
                            budget=Budget(max_duration_s=100))
    await h.start()
    await h.runtime.send("go")
    request = (await h.rec.wait_for(lambda r: r.of(PermissionRequested)))[0].request
    await asyncio.sleep(0.4)
    await h.runtime.resolve_permission(request.id, Decision.ALLOW_ONCE)
    await h.rec.wait_state(AgentState.IDLE, count=2)
    assert h.runtime.snapshot().usage.elapsed_s < 0.3


async def test_builtins_not_counted(make: Any) -> None:
    tools = FakeTools()
    tools.instant("t")
    core = ScriptedCore([
        Step(calls=[call("recall", {"query": "x"}), call("op_status", {"call_id": "zz"})]),
        Step(calls=[call("t")]),
        Step(text="ok"),
    ])
    h: Harness = await make(core=core, tools=tools, budget=Budget(max_tool_calls=5))
    await h.start()
    await h.turn("go")
    assert h.runtime.snapshot().usage.tool_calls == 1
    assert h.runtime.snapshot().usage.budget_remaining["max_tool_calls"] == 4


async def test_tool_refused_when_exhausted(make: Any) -> None:
    tools = FakeTools()
    tools.instant("t")
    core = ScriptedCore([Step(calls=[call("t", id="t1")], usage=Usage(1000, 200))])
    h: Harness = await make(core=core, tools=tools, budget=Budget(max_tokens=1000))
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_state(AgentState.FAILED)
    result = (await h.results())["t1"]
    assert result.status == ToolStatus.REFUSED and "budget" in result.content.lower()
    assert tools.started == []
    assert len(h.core.requests) == 1
    assert h.rec.of(Failure)[-1].kind == FailureKind.BUDGET


async def test_mid_stream_exhaustion(make: Any) -> None:
    core = ScriptedCore([Step(text="stream", reports=[Usage(1000, 600)], hang=True)])
    h: Harness = await make(core=core, budget=Budget(max_tokens=1500))
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_state(AgentState.FAILED, timeout=2)
    assert core.cancelled == 1
    assert h.rec.of(Failure)[-1].kind == FailureKind.BUDGET
    usage = h.runtime.snapshot().usage.usage
    assert usage.input_tokens >= 1000 and usage.output_tokens >= 600


async def test_failed_request_charged(make: Any) -> None:
    core = ScriptedCore([Step(text="abc", reports=[Usage(100_000, 5)],
                              error=TransientCoreError("reset"))])
    h: Harness = await make(core=core, max_retries=0)
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_state(AgentState.FAILED)
    usage = h.runtime.snapshot().usage.usage
    assert usage.input_tokens == 100_000  # report beats the estimate
    assert usage.output_tokens == 5

    core.add(Step(error=TransientCoreError("reset")))  # no report: estimate is charged
    await h.runtime.activate()
    await h.rec.wait_state(AgentState.FAILED, count=2)
    assert h.runtime.snapshot().usage.usage.input_tokens > 100_000


async def test_usage_persists(make: Any) -> None:
    core = ScriptedCore(default=Step(text="ok", usage=Usage(500, 50)))
    h: Harness = await make(core=core)
    await h.start()
    await h.turn("a")
    await h.close()
    data = json.loads((h.data_dir / "usage-session.json").read_text())
    assert data["input_tokens"] == 500 and data["output_tokens"] == 50

    h2: Harness = await make(data_dir=h.data_dir, store=h.store)
    assert h2.runtime.snapshot().usage.usage.input_tokens == 0  # not loaded before start
    await h2.start()
    assert h2.runtime.snapshot().usage.usage.input_tokens == 500

    h3: Harness = await make(data_dir=h.data_dir)
    await h3.start(Mode.CONTINUOUS)  # separate per mode
    assert h3.runtime.snapshot().usage.usage.input_tokens == 0
