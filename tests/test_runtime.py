"""Permissions, instance safety, events, retries, stop and idle behavior."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from typing import Any

import pytest

from kayden.api import (
    PERMISSION_CEILING_ENV,
    AgentState,
    ContextOverflowError,
    Decision,
    InstanceLockedError,
    MessageCommitted,
    MessageQueued,
    ModelRetry,
    ModelSelected,
    OperationResult,
    PermissionPolicy,
    PermissionRequested,
    PermissionResolved,
    Role,
    ShuttingDown,
    StartOptions,
    StopReason,
    StopStalled,
    Text,
    ToolCall,
    ToolFinished,
    ToolStarted,
    ToolStatus,
    TransientCoreError,
)
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools


def perm_tools() -> FakeTools:
    tools = FakeTools()
    tools.instant("read", read_only=True)
    tools.instant("write", read_only=False)
    return tools


# ── Permissions ──


async def test_permission_deny_policy(make: Any) -> None:
    core = ScriptedCore([
        Step(calls=[call("read", id="r"), call("write", id="w"),
                    call("recall", {"query": "q"}, id="b")]),
        Step(text="ok"),
    ])
    h: Harness = await make(core=core, tools=perm_tools(), permissions=PermissionPolicy.DENY)
    await h.start()
    await h.turn("go")
    results = await h.results()
    assert results["r"].status == results["w"].status == ToolStatus.REFUSED
    assert results["b"].status == ToolStatus.OK  # built-ins are never gated
    assert h.rec.of(PermissionRequested) == []
    resolved = {e.call_id: e.decision for e in h.rec.of(PermissionResolved)}
    assert resolved == {"r": Decision.DENY, "w": Decision.DENY}
    assert len({e.request_id for e in h.rec.of(PermissionResolved)}) == 2
    refused = [e for e in h.rec.of(ToolFinished) if e.result.status == ToolStatus.REFUSED]
    assert len(refused) == 2
    assert not any(e.call.id in ("r", "w") for e in h.rec.of(ToolStarted))


async def test_permission_ask_policy_and_grants(make: Any) -> None:
    core = ScriptedCore([
        Step(calls=[call("read", id="r1")]),
        Step(calls=[call("read", id="r2")]),
        Step(calls=[call("read", id="r3")]),
        Step(text="ok"),
    ])
    h: Harness = await make(core=core, tools=perm_tools(), permissions=PermissionPolicy.ASK)
    await h.start()
    await h.runtime.send("go")
    req = (await h.rec.wait_for(lambda r: r.of(PermissionRequested)))[0].request
    assert req.call.id == "r1" and req.read_only
    assert h.runtime.snapshot().state == AgentState.AWAITING_PERMISSION
    assert h.runtime.snapshot().pending_permissions == [req]
    await h.runtime.resolve_permission(req.id, Decision.ALLOW_ONCE)
    req2 = (await h.rec.wait_for(lambda r: r.of(PermissionRequested)[1:]))[0].request
    assert req2.call.id == "r2"
    await h.runtime.resolve_permission(req2.id, Decision.ALLOW_CONVERSATION)
    await h.rec.wait_state(AgentState.IDLE, count=2)
    assert len(h.rec.of(PermissionRequested)) == 2  # r3 covered by the grant
    auto = [e for e in h.rec.of(PermissionResolved) if e.call_id == "r3"]
    assert auto[0].decision == Decision.ALLOW_CONVERSATION
    assert all(r.status == ToolStatus.OK for r in (await h.results()).values())

    # Grants are per conversation.
    core.add(Step(calls=[call("read", id="r4")]), Step(text="ok"))
    await h.start()
    await h.runtime.send("new conversation")
    req4 = (await h.rec.wait_for(lambda r: r.of(PermissionRequested)[2:]))[0].request
    assert req4.call.id == "r4"
    await h.runtime.resolve_permission(req4.id, Decision.DENY)
    await h.rec.wait_state(AgentState.IDLE, count=3)
    assert (await h.results())["r4"].status == ToolStatus.REFUSED


async def test_permission_allow_read_only_policy(make: Any) -> None:
    tools = perm_tools()
    tools.slow("scan", 0.5)
    core = ScriptedCore([
        Step(calls=[call("scan", id="s"), call("read", id="r"), call("write", id="w")]),
        Step(text="ok"),
    ])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=0.1)
    await h.start()
    await h.runtime.send("go")
    req = (await h.rec.wait_for(lambda r: r.of(PermissionRequested)))[0].request
    assert req.call.id == "w" and not req.read_only
    # Read-only calls need no prompt; the backgrounded scan keeps running during the prompt.
    assert {e.call.id for e in h.rec.of(ToolStarted)} == {"s", "r"}
    snap = h.runtime.snapshot()
    assert snap.state == AgentState.AWAITING_PERMISSION
    assert [o.call.id for o in snap.operations] == ["s"]
    await h.runtime.resolve_permission(req.id, Decision.ALLOW_ONCE)
    await h.rec.wait_state(AgentState.IDLE, count=2)
    statuses = {cid: r.status for cid, r in (await h.results()).items()}
    assert statuses == {"s": ToolStatus.BACKGROUNDED, "r": ToolStatus.OK, "w": ToolStatus.OK}
    auto = {e.call_id: e.decision for e in h.rec.of(PermissionResolved)}
    assert auto["s"] == auto["r"] == Decision.ALLOW_ONCE


async def test_permission_non_interactive_frontend(make: Any) -> None:
    core = ScriptedCore([Step(calls=[call("read", id="r"), call("write", id="w")]),
                         Step(text="ok")])
    h: Harness = await make(core=core, tools=perm_tools(), interactive=False)
    await h.start()
    await h.turn("go")
    results = await h.results()
    assert results["r"].status == ToolStatus.OK
    assert results["w"].status == ToolStatus.REFUSED
    assert h.rec.of(PermissionRequested) == []


async def test_permission_stop_denies_prompts(make: Any) -> None:
    core = ScriptedCore([Step(calls=[call("write", id="w")])])
    h: Harness = await make(core=core, tools=perm_tools())
    await h.start()
    await h.runtime.send("go")
    req = (await h.rec.wait_for(lambda r: r.of(PermissionRequested)))[0].request
    await h.runtime.stop()
    await h.rec.wait_state(AgentState.STOPPED)
    resolved = h.rec.of(PermissionResolved)
    assert resolved[-1] == PermissionResolved(req.id, Decision.DENY, "w")
    assert (await h.results())["w"].status == ToolStatus.REFUSED
    assert h.runtime.snapshot().pending_permissions == []


async def test_permission_ceiling(make: Any, clean_env: pytest.MonkeyPatch) -> None:
    h: Harness = await make(permissions=PermissionPolicy.ASK)
    assert os.environ[PERMISSION_CEILING_ENV] == "ask"
    await h.close()

    clean_env.setenv(PERMISSION_CEILING_ENV, "deny")
    tools = perm_tools()
    tools.env_probe()
    core = ScriptedCore([Step(calls=[call("env"), call("read", id="r")]), Step(text="ok")])
    h2: Harness = await make(core=core, tools=tools,
                             permissions=PermissionPolicy.ALLOW_READ_ONLY)
    await h2.start()
    snap = h2.runtime.snapshot()
    assert snap.config is not None and snap.config.permissions == PermissionPolicy.DENY
    assert os.environ[PERMISSION_CEILING_ENV] == "deny"
    await h2.turn("go")
    assert (await h2.results())["r"].status == ToolStatus.REFUSED

    clean_env.setenv(PERMISSION_CEILING_ENV, "allow_read_only")
    h3: Harness = await make(tools=perm_tools(), permissions=PermissionPolicy.ASK)
    await h3.start()
    assert h3.runtime.snapshot().config.permissions == PermissionPolicy.ASK

    clean_env.setenv(PERMISSION_CEILING_ENV, "garbage")
    h4: Harness = await make(permissions=PermissionPolicy.ALLOW_READ_ONLY)
    await h4.start()
    assert h4.runtime.snapshot().config.permissions == PermissionPolicy.DENY


async def test_subprocess_tools_see_ceiling(make: Any) -> None:
    tools = FakeTools()
    tools.env_probe()
    core = ScriptedCore([Step(calls=[call("env")]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    assert tools.envs[0][PERMISSION_CEILING_ENV] == "allow_read_only"


# ── Instance lock ──


async def test_instance_lock(make: Any) -> None:
    h: Harness = await make()
    await h.start()
    other: Harness = await make(data_dir=h.data_dir)
    with pytest.raises(InstanceLockedError, match=f"already open.*{os.getpid()}"):
        await other.start()
    await h.close()
    await other.start()  # released on shutdown


# ── Event stream and snapshot ──


async def test_event_seq_gapless_and_stream_end(make: Any) -> None:
    h: Harness = await make()
    await h.start()
    await h.turn("hi")
    await h.close()
    await h.rec.wait_for(lambda r: r.ended)
    seqs = [e.seq for e in h.rec.envelopes]
    assert seqs == list(range(1, len(seqs) + 1))
    assert isinstance(h.rec.events[-1], ShuttingDown)


async def test_event_subscription_registered_on_call(make: Any) -> None:
    h: Harness = await make()
    stream = h.runtime.events()
    await h.runtime.select_model("fake-2")
    first = await asyncio.wait_for(anext(stream), 1)
    assert first.event == ModelSelected("fake-2")


async def test_event_queue_bounded_drops_oldest(make: Any) -> None:
    h: Harness = await make(subscriber_queue_size=3)
    stream = h.runtime.events()
    for model in ("fake-2", "fake-1", "fake-2", "fake-1", "fake-2"):
        await h.runtime.select_model(model)
    got = [await asyncio.wait_for(anext(stream), 1) for _ in range(3)]
    assert [e.seq for e in got] == [3, 4, 5]


async def test_snapshot_recovery(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("work", progress="step 1")
    core = ScriptedCore([Step(calls=[call("work", id="w")])])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=10)
    snap = h.runtime.snapshot()
    assert snap.state == AgentState.NOT_STARTED and snap.conversation is None
    conv = await h.start()
    await h.runtime.send("go")
    await h.rec.wait_for(lambda r: any(getattr(e, "text", None) == "step 1" for e in r.events))
    await h.runtime.send("later")
    late = h.runtime.events()
    snap = h.runtime.snapshot()
    assert snap.seq == h.runtime.snapshot().seq
    assert snap.state == AgentState.RUNNING_TOOLS and snap.conversation.id == conv.id
    assert [(o.call.id, o.last_progress) for o in snap.operations] == [("w", "step 1")]
    assert [q.text for q in snap.queued_messages] == ["later"]
    assert snap.model == "fake-1"
    await h.runtime.stop()
    env = await asyncio.wait_for(anext(late), 1)
    assert env.seq == snap.seq + 1


async def test_jsonl_event_log(make: Any) -> None:
    tools = FakeTools()
    tools.instant("t")
    core = ScriptedCore([Step(calls=[call("t")]), Step(text="done", hang=True)])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.runtime.send("go")
    await eventually(lambda: len(core.requests) == 2)
    await h.runtime.stop()
    await h.rec.wait_state(AgentState.STOPPED)
    await h.close()
    lines = [json.loads(line) for line in
             (h.data_dir / "events.jsonl").read_text().splitlines()]
    kinds = {line.get("event") for line in lines}
    assert {"StateChanged", "ToolStarted", "ToolFinished", "MessageCommitted"} <= kinds
    decisions = {line.get("decision") for line in lines}
    assert {"budget_check", "stop", "permission"} <= decisions
    assert all("ts" in line for line in lines)


# ── Retries and overflow ──


async def test_retry_backoff_delays(make: Any) -> None:
    core = ScriptedCore([
        Step(error=TransientCoreError("a")),
        Step(error=TransientCoreError("b")),
        Step(error=TransientCoreError("c", retry_after=0.03)),
        Step(text="ok"),
    ])
    h: Harness = await make(core=core, max_retries=3, retry_base_delay_s=0.01,
                            retry_max_delay_s=0.015)
    await h.start()
    await h.turn("go")
    assert [r.delay_s for r in h.rec.of(ModelRetry)] == [0.01, 0.015, 0.03]
    assert [r.error for r in h.rec.of(ModelRetry)] == ["a", "b", "c"]


async def test_context_overflow_retries_smaller(make: Any) -> None:
    core = ScriptedCore([Step(error=ContextOverflowError("too long")), Step(text="ok")])
    h: Harness = await make(core=core)
    await h.start()
    await h.turn("go")
    first, second = h.memory.assemble_budgets[-2:]
    assert second == int(first * 0.9)
    assert len(core.requests) == 2


async def test_context_overflow_fails_after_one_retry(make: Any) -> None:
    core = ScriptedCore(default=Step(error=ContextOverflowError("too long")))
    h: Harness = await make(core=core)
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_state(AgentState.FAILED)
    assert len(core.requests) == 2


async def test_max_tokens_truncated_call(make: Any) -> None:
    tools = FakeTools()
    tools.instant("echo")
    core = ScriptedCore([
        Step(text="cut", calls=[ToolCall("c1", "echo", {}, raw_arguments='{"a": ')],
             stop=StopReason.MAX_TOKENS),
        Step(text="retrying"),
    ])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    assert (await h.results())["c1"].status == ToolStatus.ERROR
    assert len(core.requests) == 2 and tools.started == []


async def test_select_model(make: Any) -> None:
    core = ScriptedCore([Step(text="one", delay=0.1), Step(text="two")])
    h: Harness = await make(core=core)
    await h.start()
    with pytest.raises(ValueError):
        await h.runtime.select_model("nonexistent")
    await h.runtime.send("go")
    await eventually(lambda: core.requests)
    await h.runtime.select_model("fake-2")
    await h.rec.wait_state(AgentState.IDLE, count=2)
    await h.turn("again")
    assert [r.model for r in core.requests] == ["fake-1", "fake-2"]
    assert h.rec.of(ModelSelected) == [ModelSelected("fake-2")]


async def test_model_without_context_window_is_refused(make: Any) -> None:
    """A window the provider does not report is configured on the Core (its `models`
    option), not in the runtime."""
    from kayden.api import ModelInfo

    models = [ModelInfo("mystery", "Mystery", context_window=None)]
    h: Harness = await make(core=ScriptedCore(models=models))
    with pytest.raises(ValueError, match="Core's models option"):
        await h.start(model="mystery")


# ── Stop ──


async def test_stop_commits_text_only(make: Any) -> None:
    core = ScriptedCore([Step(reasoning="hmm", text="partial",
                              calls=[call("t", {"x": 1})], hang=True)])
    h: Harness = await make(core=core)
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_for(lambda r: any(type(e).__name__ == "ToolCallDelta" for e in r.events))
    await h.runtime.stop()
    await h.rec.wait_state(AgentState.STOPPED)
    assistant = (await h.transcript())[-1]
    assert assistant.role == Role.ASSISTANT and assistant.blocks == (Text("partial"),)


async def test_stop_queue_while_stopping(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("stubborn", honor_cancel=False)
    core = ScriptedCore([Step(calls=[call("stubborn", id="s")]), Step(text="after stop")])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=10, cancel_grace_s=0.4)
    await h.start()
    await h.runtime.send("go")
    await eventually(lambda: tools.started)
    await h.runtime.stop()
    await h.rec.wait_state(AgentState.STOPPING)
    await h.runtime.send("queued while stopping")
    queued = (await h.rec.wait_for(lambda r: r.of(MessageQueued)))[0]
    await h.rec.wait_state(AgentState.STOPPED)
    assert h.rec.of(StopStalled) == [StopStalled(["s"])]
    result = (await h.results())["s"]
    assert result.status == ToolStatus.ABANDONED and "may still have effects" in result.content
    await eventually(lambda: len(core.requests) == 2)
    committed = [e.message for e in h.rec.of(MessageCommitted) if e.message.id == queued.message_id]
    assert committed and committed[0].blocks == (Text("queued while stopping"),)
    stopped_at = h.rec.states.index(AgentState.STOPPED)
    assert AgentState.THINKING in h.rec.states[stopped_at:]


async def test_stop_in_idle_with_background_ops(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("bg")
    core = ScriptedCore([Step(calls=[call("bg", id="bg")]), Step(text="waiting")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    assert h.runtime.snapshot().state == AgentState.IDLE
    await h.runtime.stop()
    await h.rec.wait_state(AgentState.STOPPED)
    ops = [b for m in await h.transcript() for b in m.blocks if isinstance(b, OperationResult)]
    assert [(o.call_id, o.status) for o in ops] == [("bg", ToolStatus.CANCELLED)]
    await asyncio.sleep(0.2)
    assert len(core.requests) == 2  # wake-ups disabled


# ── Idle ──


async def test_idle_unseen_results_wake_immediately(make: Any) -> None:
    tools = FakeTools()
    tools.slow("job", 0.25)
    core = ScriptedCore([
        Step(calls=[call("job", id="j")]),
        Step(text="musing", chunks=4, delay=0.1),  # job ends mid-inference
        Step(text="saw the job result"),
    ])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.runtime.send("go")
    await eventually(lambda: len(core.requests) >= 2)
    await asyncio.sleep(0.8)
    assert len(core.requests) == 3





async def test_session_switch_conversation(make: Any) -> None:
    h: Harness = await make()
    first = await h.start()
    await h.turn("hello")
    second = await h.start()
    assert second.id != first.id
    reopened = await h.start(conversation_id=first.id)
    assert reopened.id == first.id
    with pytest.raises(KeyError):
        await h.runtime.start(StartOptions(conversation_id="missing"))


async def test_a_finished_background_task_wakes_the_agent(make: Any) -> None:
    """The agent started the work for the user, so it reports when the work is done
    without waiting to be asked."""
    tools = FakeTools()
    tools.slow("bg", 0.4)
    core = ScriptedCore([Step(calls=[call("bg", id="bg")]), Step(text="started it"),
                         Step(text="it finished")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    await eventually(lambda: len(core.requests) == 3)
    woken = core.requests[2].messages[-1]
    assert woken.role == Role.USER and woken.blocks == (
        OperationResult("bg", ToolStatus.OK, "bg finished"),)
    await h.rec.wait_for(lambda r: any(
        isinstance(e, MessageCommitted) and e.message.blocks == (Text("it finished"),)
        for e in r.events))
    await asyncio.sleep(0.2)
    assert len(core.requests) == 3  # woken once


async def test_shutdown_cancels_calls(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("bg")
    core = ScriptedCore([Step(calls=[call("bg", id="bg")]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    await h.close()
    conv = h.store.conversations[h.conversation.id]
    ops = [b for m in conv.messages for b in m.blocks if isinstance(b, OperationResult)]
    assert [(o.call_id, o.status) for o in ops] == [("bg", ToolStatus.CANCELLED)]
    assert isinstance(h.rec.events[-1], ShuttingDown)


async def test_shutdown_abandons_calls_that_ignore_cancel(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("stubborn", honor_cancel=False)
    core = ScriptedCore([Step(calls=[call("stubborn", id="s")]), Step(text="ok")])
    h: Harness = await make(core=core, tools=tools, cancel_grace_s=0.2)
    await h.start()
    await h.turn("go")
    await h.runtime.aclose()  # h.close() would first release the stubborn tool
    await h.close()
    conv = h.store.conversations[h.conversation.id]
    [op] = [b for m in conv.messages for b in m.blocks if isinstance(b, OperationResult)]
    assert (op.call_id, op.status) == ("s", ToolStatus.ABANDONED)
    assert "may still have effects" in op.content
    assert isinstance(h.rec.events[-1], ShuttingDown)


async def test_stop_before_cycle_starts(make: Any) -> None:
    h: Harness = await make()
    await h.start()
    await h.runtime.send("never answered")
    await h.runtime.stop()
    await h.rec.wait_state(AgentState.STOPPED)
    await asyncio.sleep(0.1)
    assert h.core.requests == []
    assert [m.blocks for m in await h.transcript()] == [(Text("never answered"),)]


async def test_stop_keeps_message_queued_before_stop(make: Any) -> None:
    core = ScriptedCore([Step(text="thinking", hang=True)])
    h: Harness = await make(core=core)
    await h.start()
    await h.runtime.send("first")
    await eventually(lambda: core.requests)
    await h.runtime.send("second")
    await h.runtime.stop()
    await h.rec.wait_state(AgentState.STOPPED)
    await asyncio.sleep(0.1)
    assert len(core.requests) == 1
    texts = [b.text for m in await h.transcript() for b in m.blocks if isinstance(b, Text)]
    assert texts == ["first", "thinking", "second"]


async def test_message_queued_before_stop_appended_at_stopped(make: Any) -> None:
    core = ScriptedCore([Step(text="thinking", hang=True)])
    h: Harness = await make(core=core)
    await h.start()
    await h.runtime.send("first")
    await eventually(lambda: core.requests)
    await h.runtime.send("before stop")
    queued = (await h.rec.wait_for(lambda r: r.of(MessageQueued)))[0]
    await h.runtime.stop()
    await h.rec.wait_state(AgentState.STOPPED)
    committed = [i for i, env in enumerate(h.rec.envelopes)
                 if isinstance(env.event, MessageCommitted)
                 and env.event.message.id == queued.message_id]
    stopped = [i for i, env in enumerate(h.rec.envelopes)
               if getattr(env.event, "state", None) == AgentState.STOPPED]
    assert committed and committed[0] < stopped[0]
    await asyncio.sleep(0.1)
    assert len(core.requests) == 1
    assert AgentState.THINKING not in h.rec.states[h.rec.states.index(AgentState.STOPPED):]
    assert (await h.transcript())[-1].id == queued.message_id


async def test_crash_loses_only_the_queued_message(make: Any) -> None:
    core = ScriptedCore([Step(text="answer"), Step(text="thinking", hang=True)])
    h: Harness = await make(core=core)
    info = await h.start()
    await h.turn("first")
    await h.runtime.send("second")
    await eventually(lambda: len(core.requests) == 2)
    await h.runtime.send("lost")
    await h.rec.wait_for(lambda r: r.of(MessageQueued))
    crashed = copy.deepcopy(h.store)  # the archive as a crash would leave it

    h2: Harness = await make(store=crashed)
    await h2.start(conversation_id=info.id)
    texts = [b.text for m in await h2.transcript() for b in m.blocks if isinstance(b, Text)]
    assert texts == ["first", "answer", "second"]


async def test_maintenance_model(make: Any) -> None:
    from kayden.api import Completion, Message

    bad: Harness = await make(maintenance_model="nonexistent")
    with pytest.raises(ValueError):
        await bad.start()

    h: Harness = await make(maintenance_model="fake-2")
    windows: list[int] = []

    async def maintain(complete: Completion) -> bool:
        windows.append(complete.max_input_tokens(light=True))
        await complete("Summarize.", [Message("m", Role.USER, (Text("x"),), 0.0)],
                       max_tokens=20, light=True)
        return True

    h.memory.maintain_override = maintain
    await h.start()
    await h.turn("go")
    # maintain runs before the answer and once after it
    assert windows == [50_000, 50_000]
    assert [r.model for r in h.core.requests] == ["fake-2", "fake-1", "fake-2"]
    from kayden.api import MaintenanceFinished, MaintenanceStarted

    assert len(h.rec.of(MaintenanceStarted)) == len(h.rec.of(MaintenanceFinished)) == 2


@pytest.mark.parametrize("summary", ["SUMMARY: short", ""])
async def test_compaction_is_announced(make: Any, summary: str) -> None:
    """Frontends show a compaction while it runs; other maintenance stays quiet."""
    from kayden.api import CompactionFinished, CompactionStarted, Completion, Message

    def step(request: Any) -> Step:
        return Step(text=summary if request.maintenance else "ok")

    h: Harness = await make(core=ScriptedCore(default=step))
    first = Message("m", Role.USER, (Text("x"),), 0.0)
    passes = 0

    async def maintain(complete: Completion) -> bool:
        nonlocal passes
        passes += 1
        await complete("Save.", [first], max_tokens=20, light=True)
        if passes == 1:
            return bool(await complete("Summarize.", [first], max_tokens=20, light=True,
                                       compacting=True))
        return False

    h.memory.maintain_override = maintain
    conv = await h.start()
    await h.turn("go")
    assert h.rec.of(CompactionStarted) == [CompactionStarted(conv.id)]
    assert h.rec.of(CompactionFinished) == [CompactionFinished(conv.id, bool(summary))]


async def test_completion_caps_max_tokens_at_the_model_limit(make: Any) -> None:
    from kayden.api import Completion, Message

    h: Harness = await make(maintenance_model="fake-2")  # max_output_tokens 1000

    async def maintain(complete: Completion) -> bool:
        for light, asked in ((True, 5_000), (True, 300)):
            await complete("Summarize.", [Message("m", Role.USER, (Text("x"),), 0.0)],
                           max_tokens=asked, light=light)
        return True

    h.memory.maintain_override = maintain
    await h.start()
    await h.turn("go")
    asked = [r.max_output_tokens for r in h.core.requests if r.maintenance]
    assert asked == [1_000, 300] * 2  # before the answer and after it


async def test_only_completion_requests_are_maintenance(make: Any) -> None:
    """Requests made through Memory's Completion are marked maintenance; turns, retries and
    overflow recovery are not, whatever tools they carry."""
    from kayden.api import Completion, Message

    core = ScriptedCore([Step(error=ContextOverflowError("too long")),
                         Step(calls=[call("recall", {"query": "x"})]),
                         Step(error=TransientCoreError("busy"))])
    h: Harness = await make(core=core, tools=FakeTools(),
                            permissions=PermissionPolicy.DENY)

    async def maintain(complete: Completion) -> bool:
        await complete("Summarize.", [Message("m", Role.USER, (Text("x"),), 0.0)], max_tokens=20)
        return False

    h.memory.maintain_override = maintain
    await h.start()
    await h.turn("go")
    await h.runtime.activate()
    await eventually(lambda: h.runtime.snapshot().state == AgentState.IDLE)
    requests = core.requests
    assert len(requests) >= 6
    assert any(r.system == "Summarize." for r in requests)
    for request in requests:
        assert request.maintenance == (request.system == "Summarize."), request


# ── Activation notice, stop reason ──


async def test_activate_appends_notice_after_assistant(make: Any) -> None:
    h: Harness = await make()
    await h.start()
    await h.turn("hi")
    await h.runtime.activate()
    await h.rec.wait_state(AgentState.IDLE, count=3)
    transcript = await h.transcript()
    assert [m.role for m in transcript] == [Role.USER, Role.ASSISTANT, Role.USER, Role.ASSISTANT]
    notice = transcript[2]
    assert "runtime notice" in notice.blocks[0].text.lower()
    assert h.core.requests[1].messages[-1].id == notice.id


async def test_activate_empty_conversation(make: Any) -> None:
    h: Harness = await make()
    await h.start()
    await h.runtime.activate()
    await h.rec.wait_state(AgentState.IDLE, count=2)
    transcript = await h.transcript()
    assert [m.role for m in transcript] == [Role.USER, Role.ASSISTANT]
    assert h.core.requests[0].messages[0].role == Role.USER


async def test_activate_after_user_message_adds_no_notice(make: Any) -> None:
    from kayden.api import CoreError

    core = ScriptedCore([Step(error=CoreError("boom")), Step(text="ok")])
    h: Harness = await make(core=core)
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_state(AgentState.FAILED)
    await h.runtime.activate()
    await h.rec.wait_state(AgentState.IDLE, count=2)
    assert [m.role for m in await h.transcript()] == [Role.USER, Role.ASSISTANT]


async def test_activate_after_tool_message_adds_no_notice(make: Any) -> None:
    from kayden.api import CoreError

    tools = FakeTools()
    tools.instant("t")
    core = ScriptedCore([Step(calls=[call("t")]), Step(error=CoreError("boom")),
                         Step(text="ok")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.runtime.send("go")
    await h.rec.wait_state(AgentState.FAILED)
    await h.runtime.activate()
    await h.rec.wait_state(AgentState.IDLE, count=2)
    assert [m.role for m in await h.transcript()] == [Role.USER, Role.ASSISTANT, Role.TOOL,
                                                      Role.ASSISTANT]
    assert h.core.requests[-1].messages[-1].role == Role.TOOL


async def test_message_committed_carries_stop_reason(make: Any) -> None:
    tools = FakeTools()
    tools.instant("t")
    core = ScriptedCore([Step(calls=[call("t")]), Step(text="done")])
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")
    stops = [(e.message.role, e.stop) for e in h.rec.of(MessageCommitted)]
    assert stops == [(Role.USER, None), (Role.ASSISTANT, StopReason.TOOL_USE),
                     (Role.TOOL, None), (Role.ASSISTANT, StopReason.END_TURN)]


async def test_memory_upkeep_runs_after_the_answer_and_saves_nothing_itself(
        make: Any) -> None:
    """Memory runs its upkeep (a title, compaction) once more after the answer; what to
    remember is the agent's to write with the memory tool, so nothing is announced."""
    from kayden.api import Completion, MemoriesChanged

    h: Harness = await make()
    seen: list[str] = []

    async def maintain(complete: Completion) -> bool:
        transcript = await h.memory.history(h.runtime.snapshot().conversation.id)
        seen.append(transcript[-1].role.value)
        return True

    h.memory.maintain_override = maintain
    await h.start()
    await h.turn("go")
    assert seen == ["user", "assistant"]  # before the answer, then once after it
    assert h.rec.of(MemoriesChanged) == []
