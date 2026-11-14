"""Acceptance scenarios A-L (SPEC section 12)."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from kadence.api import (
    AgentState,
    Budget,
    Completed,
    Core,
    CoreError,
    CoreEvent,
    Failure,
    FailureKind,
    InferenceRequest,
    Message,
    MessageCommitted,
    ModelInfo,
    ModelRetry,
    OperationResult,
    Role,
    RuntimeAPI,
    RuntimeStateError,
    StartOptions,
    StateChanged,
    StopReason,
    Text,
    ToolBackgrounded,
    ToolFinished,
    ToolOutput,
    ToolSpec,
    ToolStatus,
    TransientCoreError,
    Usage,
)
from kadence.runtime import InstanceConfig, ModuleSpec, RuntimeConfig, run_instance
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.frontend import ScriptedFrontend
from tests.fakes.memory import MemoryStore, render
from tests.fakes.tools import FakeTools


def op_results(messages: Sequence[Message]) -> list[OperationResult]:
    return [b for m in messages for b in m.blocks if isinstance(b, OperationResult)]


def last_text(messages: Sequence[Message]) -> str:
    return render([m for m in messages if m.role == Role.ASSISTANT][-1])


# ── A: replaceable modules ──


class EchoCore(Core):
    """A second, independent Core implementation."""

    async def list_models(self) -> Sequence[ModelInfo]:
        return [ModelInfo("echo", "Echo", context_window=10_000)]

    async def stream(self, request: InferenceRequest) -> AsyncIterator[CoreEvent]:
        last = request.messages[-1]
        text = "echo: " + render(last).split("] ", 1)[-1]  # without the time it was sent
        yield Completed(
            Message(f"echo-{len(request.messages)}", Role.ASSISTANT, (Text(text),), 0.0),
            Usage(1, 1),
            StopReason.END_TURN,
        )


def echo_core(options: dict[str, Any], paths: Any) -> EchoCore:
    return EchoCore()


@pytest.mark.parametrize(
    ("core_path", "options", "expected"),
    [
        ("tests.fakes.core:factory", {"default": Step(text="scripted reply")}, "scripted reply"),
        ("tests.test_scenarios:echo_core", {}, "echo: hello"),
    ],
)
async def test_a_replaceable_modules(
    tmp_path: Path, clean_env: Any, core_path: str, options: dict[str, Any], expected: str
) -> None:
    store = MemoryStore()
    seen: dict[str, Any] = {}

    async def script(runtime: RuntimeAPI, fe: ScriptedFrontend) -> None:
        assert fe.recorder is not None
        conv = await runtime.start(StartOptions())
        await runtime.send("hello")
        await fe.recorder.wait_state(AgentState.IDLE, count=2)
        seen["history"] = await runtime.history(conv.id)

    config = InstanceConfig(
        runtime=RuntimeConfig(data_dir=tmp_path),
        core=ModuleSpec(core_path, options),
        memory=ModuleSpec("tests.fakes.memory:factory", {"store": store}),
        tools=ModuleSpec("tests.fakes.tools:factory", {"instant": ["lookup"]}),
        frontend=ModuleSpec("tests.fakes.frontend:factory", {"script": script}),
    )
    await run_instance(config)
    assert last_text(seen["history"]) == expected
    assert (tmp_path / "instance.lock").exists()


# ── B: session conversation ──


async def test_b_session_conversation(make: Any) -> None:
    tools = FakeTools()
    tools.instant("lookup", "found it")
    core = ScriptedCore([Step(calls=[call("lookup", {"q": "x"})]), Step(text="the answer")])
    h: Harness = await make(core=core, tools=tools)
    conv = await h.start()
    await h.turn("question")
    before = await h.transcript()
    assert [m.role for m in before] == [Role.USER, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT]
    assert last_text(before) == "the answer"
    await h.close()

    h2: Harness = await make(store=h.store, data_dir=h.data_dir,
                             core=ScriptedCore(default=Step(text="second")))
    assert conv.id in [c.id for c in await h2.runtime.list_conversations()]
    await h2.start(conversation_id=conv.id)
    assert list(await h2.runtime.history(conv.id)) == before
    await h2.turn("follow-up")
    request = h2.core.requests[-1]
    assert [m.id for m in request.messages][: len(before)] == [m.id for m in before]


# ── C: background work wakes the agent ──


async def test_c_background_work_wakes_the_agent(make: Any) -> None:
    tools = FakeTools()
    tools.slow("fetch", 0.4)
    core = ScriptedCore([
        Step(text="let me fetch", calls=[call("fetch", id="f1")]),
        Step(text="fetching, back soon"),
        Step(text="got the result"),
    ])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_for(lambda r: any(
        isinstance(e, MessageCommitted) and e.message.blocks == (Text("got the result"),)
        for e in r.events))
    transcript = await h.transcript()
    assert [m.role for m in transcript] == [
        Role.USER, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT, Role.USER, Role.ASSISTANT,
    ]
    assert transcript[2].blocks[0].status == ToolStatus.BACKGROUNDED
    assert op_results(transcript)[0].status == ToolStatus.OK
    assert any(isinstance(b, OperationResult) for m in core.requests[2].messages
               for b in m.blocks)
    await asyncio.sleep(0.3)
    assert len(core.requests) == 3  # implicit idle with nothing pending: no re-prompt


# ── D: long-running tools ──


async def test_d_long_running_tool(make: Any) -> None:
    tools = FakeTools()
    tools.slow("build", 0.8, steps=8)
    core = ScriptedCore([
        Step(calls=[call("build", id="b1")]),
        Step(calls=[call("op_status", {"call_id": "b1"})]),
        Step(text="doing other work", calls=[call("op_status", {"call_id": "b1", "wait_s": 3})]),
        Step(text="all done"),
    ])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("build it")
    results = await h.results()
    assert results["b1"].status == ToolStatus.BACKGROUNDED
    assert "b1" in results["b1"].content
    assert h.rec.of(ToolBackgrounded) == [ToolBackgrounded("b1")]
    statuses = [r for r in results.values() if r.call_id not in ("b1",)]
    assert "running" in statuses[0].content and "build" in statuses[0].content
    assert "build finished" in statuses[1].content
    ops = op_results(await h.transcript())
    assert [(o.call_id, o.status) for o in ops] == [("b1", ToolStatus.OK)]
    finished = [e for e in h.rec.of(ToolFinished) if e.result.call_id == "b1"]
    assert len(finished) == 1 and finished[0].result.status == ToolStatus.OK


# ── E: idle batch wake-up ──


def batch_tools() -> FakeTools:
    tools = FakeTools()
    tools.slow("a", 0.3)
    tools.slow("b", 0.45)
    tools.slow("c", 0.6)
    return tools


def batch_core() -> ScriptedCore:
    return ScriptedCore([
        Step(calls=[call("a", id="a"), call("b", id="b"), call("c", id="c")]),
        Step(text="waiting for the batch"),
        Step(text="batch complete"),
    ])


async def test_e_batch_wakeup(make: Any) -> None:
    h: Harness = await make(core=batch_core(), tools=batch_tools())
    await h.start()
    await h.runtime.send("run the batch")
    await eventually(lambda: len(h.core.requests) == 3)
    assert h.tools.max_running == 3
    # Woken once, after all three reached terminal outcomes.
    finished_seq = max(env.seq for env in h.rec.envelopes
                       if isinstance(env.event, ToolFinished))
    changes = [env for env in h.rec.envelopes if isinstance(env.event, StateChanged)]
    wakes = [cur.seq for prev, cur in itertools.pairwise(changes)
             if prev.event.state == AgentState.IDLE and cur.event.state == AgentState.THINKING]
    assert len(wakes) == 2 and wakes[1] > finished_seq  # user send, then one batch wake
    woken = h.core.requests[2]
    assert {o.call_id for o in op_results(woken.messages)} == {"a", "b", "c"}


# ── F: stop ──


async def test_f_stop_during_inference(make: Any) -> None:
    core = ScriptedCore([Step(text="partial", hang=True)])
    h: Harness = await make(core=core)
    await h.start()
    await h.runtime.send("think hard")
    await h.rec.wait_for(lambda r: any(getattr(e, "text", None) == "partial" for e in r.events))
    await h.runtime.stop()
    await h.rec.wait_state(AgentState.STOPPED)
    assert AgentState.STOPPING in h.rec.states
    assert core.cancelled == 1
    transcript = await h.transcript()
    assert [m.role for m in transcript] == [Role.USER, Role.ASSISTANT]
    assert transcript[1].blocks == (Text("partial"),)
    await asyncio.sleep(0.2)
    assert len(core.requests) == 1


async def test_f_stop_during_tools(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("work", progress="halfway")
    core = ScriptedCore([Step(calls=[call("work", id="w1")])])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=10)
    await h.start()
    await h.runtime.send("do work")
    await eventually(lambda: tools.started)
    await h.runtime.stop()
    await h.rec.wait_state(AgentState.STOPPED)
    assert tools.cancelled == ["w1"]
    result = (await h.results())["w1"]
    assert result.status == ToolStatus.CANCELLED and "halfway" in result.content
    await asyncio.sleep(0.2)
    assert len(core.requests) == 1
    assert h.runtime.snapshot().state == AgentState.STOPPED


# ── G: restart persistence ──


def summarizing_core(reply: str = "noted") -> ScriptedCore:
    def step(request: InferenceRequest) -> Step:
        if request.maintenance:
            return Step(text="SUMMARY: " + render(request.messages[-1])[-80:])
        return Step(text=reply)

    return ScriptedCore(default=step)


async def test_g_restart_persistence(make: Any) -> None:
    h: Harness = await make(core=summarizing_core(), memory_kw={"compact_at": 0.002})
    conv = await h.start()
    await h.turn("remember the harbour code 7731 " + "x" * 400)
    for i in range(4):
        await h.turn(f"filler message {i} " + "y" * 400)
    stored = h.store.conversations[conv.id]
    assert stored.compactions >= 1
    before = await h.transcript()
    view = await h.runtime.inspect_memory(conv.id)
    await h.close()

    h2: Harness = await make(store=h.store, data_dir=h.data_dir, core=summarizing_core())
    conv2 = await h2.start(conv.id)
    assert conv2.id == conv.id
    assert h2.runtime.snapshot().state == AgentState.IDLE
    assert list(await h2.runtime.history(conv.id)) == before
    restored = await h2.runtime.inspect_memory(conv.id)
    assert restored == view and restored is not None and restored.summary.startswith("SUMMARY")
    hits = await h2.runtime.search_archive("harbour code")
    assert hits and "7731" in render(hits[0].message)
    await asyncio.sleep(0.2)
    assert h2.core.requests == []  # a resumed conversation waits for the user


# ── H: historical recall ──


async def test_h_historical_recall(make: Any) -> None:
    def step(request: InferenceRequest) -> Step:
        if not request.tools:
            return Step(text="summary")
        last = request.messages[-1]
        if last.role == Role.TOOL:
            return Step(text="found it")
        if "what was the secret" in render(last):
            return Step(calls=[call("recall", {"query": "secret launch code"}, id="r1")])
        return Step(text="ok")

    h: Harness = await make(core=ScriptedCore(default=step), memory_kw={"compact_at": 0.002})
    conv = await h.start()
    await h.turn("the secret launch code is 4417")
    for i in range(6):
        await h.turn(f"unrelated chatter {i} " + "z" * 400)
    assert h.store.conversations[conv.id].compactions >= 2
    await h.turn("what was the secret?")
    recall_request = next(r for r in reversed(h.core.requests)
                          if r.tools and r.messages[-1].role == Role.USER
                          and "what was the secret" in render(r.messages[-1]))
    assert not any("4417" in render(m) for m in recall_request.messages)
    result = (await h.results())["r1"]
    assert result.status == ToolStatus.OK and "4417" in result.content


# ── I: recall across conversations ──


async def test_i_recall_across_conversations(make: Any) -> None:
    core = ScriptedCore([
        Step(text="noted"),
        Step(calls=[call("recall", {"query": "falcon launches", "scope": "all"}, id="p1")]),
        Step(text="tuesday it is"),
    ])
    h: Harness = await make(core=core)
    first = await h.start()
    await h.turn("project falcon launches on tuesday")
    first_history = await h.transcript()

    await h.start()
    await h.turn("when does falcon launch?")
    result = (await h.results())["p1"]
    assert result.status == ToolStatus.OK and "tuesday" in result.content
    assert list(await h.runtime.history(first.id)) == first_history  # read, never changed


# ── J: multi-instance workforce ──


async def test_j_multi_instance_workforce(make: Any) -> None:
    board: list[str] = []

    def mailbox() -> FakeTools:
        tools = FakeTools()

        async def post(c: Any, ctx: Any) -> ToolOutput:
            board.append(c.arguments["text"])
            return ToolOutput("posted")

        async def read(c: Any, ctx: Any) -> ToolOutput:
            return ToolOutput(json.dumps(board))

        schema = {"type": "object", "properties": {"text": {"type": "string"}}}
        tools.add(ToolSpec("post", "Post to the shared board", schema, True), post)
        tools.add(ToolSpec("read", "Read the shared board", {"type": "object"}, True), read)
        return tools

    roles = {
        "lead": [Step(calls=[call("post", {"text": "research topic X"})]), Step(text="ok")],
        "assistant": [Step(calls=[call("read", id="r")]),
                      Step(calls=[call("post", {"text": "findings on X"})]), Step(text="ok")],
        "engineer": [Step(calls=[call("read", id="r")]), Step(text="ok")],
    }
    budgets = {"lead": 10_000, "assistant": 20_000, "engineer": 30_000}
    harnesses = {}
    for name, steps in roles.items():
        harnesses[name] = await make(core=ScriptedCore(steps), tools=mailbox(),
                                     budget=Budget(max_tokens=budgets[name]))
        await harnesses[name].start()
    for name in roles:
        await harnesses[name].turn(f"you are the {name}")

    assert board == ["research topic X", "findings on X"]
    assert "findings on X" in (await harnesses["engineer"].results())["r"].content
    dirs = {h.data_dir for h in harnesses.values()}
    assert len(dirs) == 3
    for name, h in harnesses.items():
        snap = h.runtime.snapshot()
        assert snap.config is not None and snap.config.budget.max_tokens == budgets[name]
        assert len(h.store.conversations) == 1
        assert (h.data_dir / "usage.json").exists()
    usage = {n: h.runtime.snapshot().usage.usage.input_tokens for n, h in harnesses.items()}
    assert usage["assistant"] > usage["lead"]  # three inferences vs two, counted separately


# ── K: model failure ──


async def test_k_transient_then_success(make: Any) -> None:
    core = ScriptedCore([
        Step(error=TransientCoreError("rate limited")),
        Step(text="half", error=TransientCoreError("503")),
        Step(text="fine"),
    ])
    h: Harness = await make(core=core, max_retries=3)
    await h.start()
    await h.turn("hi")
    retries = h.rec.of(ModelRetry)
    assert [(r.attempt, r.max_attempts) for r in retries] == [(1, 4), (2, 4)]
    assert last_text(await h.transcript()) == "fine"
    assert {r.model for r in core.requests} == {"fake-1"}


async def test_k_persistent_failure(make: Any) -> None:
    core = ScriptedCore(default=Step(error=TransientCoreError("overloaded")))
    h: Harness = await make(core=core, max_retries=2)
    await h.start()
    await h.runtime.send("hi")
    await h.rec.wait_state(AgentState.FAILED)
    assert len(core.requests) == 3
    assert {r.model for r in core.requests} == {"fake-1"}  # never switched
    failure = h.rec.of(Failure)[-1]
    assert failure.kind == FailureKind.MODEL and "overloaded" in failure.message
    assert [m.role for m in await h.transcript()] == [Role.USER]


async def test_k_permanent_error_not_retried(make: Any) -> None:
    core = ScriptedCore(default=Step(error=CoreError("invalid api key")))
    h: Harness = await make(core=core)
    await h.start()
    await h.runtime.send("hi")
    await h.rec.wait_state(AgentState.FAILED)
    assert len(core.requests) == 1 and h.rec.of(ModelRetry) == []


# ── L: budget enforcement ──


async def test_l_budget_enforcement(make: Any) -> None:
    core = ScriptedCore(default=Step(text="ok", usage=Usage(5000, 100)))
    h: Harness = await make(core=core, budget=Budget(max_tokens=10_000))
    await h.start()
    await h.turn("one")
    await h.turn("two")
    await h.runtime.send("three")
    await h.rec.wait_state(AgentState.FAILED)
    assert len(core.requests) == 2
    failure = h.rec.of(Failure)[-1]
    assert failure.kind == FailureKind.BUDGET and "max_tokens" in failure.message
    with pytest.raises(RuntimeStateError):
        await h.runtime.send("four")
    with pytest.raises(RuntimeStateError):
        await h.runtime.activate()
    assert len(core.requests) == 2
    assert any(isinstance(e, MessageCommitted) for e in h.rec.events)
