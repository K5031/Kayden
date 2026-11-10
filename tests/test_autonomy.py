"""Autonomy as a conversation option, fresh starts, continuity, and conversation and
long-term memory management (Agent Note: autonomy into sessions)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kayden.api import (
    AgentState,
    ConversationDeleted,
    ConversationUpdated,
    InferenceRequest,
    MemoriesChanged,
    RuntimeStateError,
    StartOptions,
    ToolFinished,
    ToolStatus,
)
from kayden.runtime.config import AUTONOMY_PROMPT
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools
from tests.test_scenarios import batch_core, batch_tools, op_results


async def decisions(h: Harness) -> list[dict[str, Any]]:
    """The event log, complete once the instance has shut down."""
    await h.close()
    path = h.data_dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


async def settled(h: Harness) -> None:
    """Wait until the recorder has every event emitted so far."""
    seq = h.runtime.snapshot().seq
    await h.rec.wait_for(lambda r: r.envelopes and r.envelopes[-1].seq >= seq)


# ── autonomy ──


async def test_autonomous_conversation_wakes_once_after_the_batch(make: Any) -> None:
    h: Harness = await make(core=batch_core(), tools=batch_tools())
    conv = await h.start(autonomous=True)
    assert conv.autonomous
    await h.runtime.send("run the batch")
    await eventually(lambda: len(h.core.requests) == 3)
    first = h.core.requests[0]
    assert "idle" in {t.name for t in first.tools}
    assert AUTONOMY_PROMPT in first.system
    assert {o.call_id for o in op_results(h.core.requests[2].messages)} == {"a", "b", "c"}
    await asyncio.sleep(0.2)
    assert len(h.core.requests) == 3


async def test_ordinary_conversation_has_no_idle_and_never_wakes(make: Any) -> None:
    core = ScriptedCore([
        Step(calls=[call("a", id="a"), call("b", id="b"), call("c", id="c")]),
        Step(calls=[call("idle", id="i")]),
        Step(text="waiting for the batch"),
    ])
    h: Harness = await make(core=core, tools=batch_tools())
    conv = await h.start()
    assert not conv.autonomous
    await h.runtime.send("run the batch")
    await h.rec.wait_for(lambda r: len(r.of(ToolFinished)) >= 4)
    assert "idle" not in {t.name for t in core.requests[0].tools}
    assert AUTONOMY_PROMPT not in core.requests[0].system
    assert (await h.results())["i"].status == ToolStatus.ERROR  # an unknown tool here
    await asyncio.sleep(0.3)
    assert len(core.requests) == 3
    assert len(op_results(await h.transcript())) == 3  # results wait for the user


async def test_turning_autonomy_off_drops_the_pending_wake_up(make: Any) -> None:
    h: Harness = await make(core=batch_core(), tools=batch_tools())
    await h.start(autonomous=True)
    await h.runtime.send("run the batch")
    await eventually(lambda: len(h.core.requests) == 2
                     and h.runtime.snapshot().state == AgentState.IDLE)
    await h.runtime.set_autonomous(False)
    await settled(h)
    await h.rec.wait_for(lambda r: len([e for e in r.of(ToolFinished)
                                        if e.result.call_id in "abc"]) == 3)
    await asyncio.sleep(0.3)
    assert len(h.core.requests) == 2
    assert h.runtime.snapshot().conversation.autonomous is False
    updated = h.rec.of(ConversationUpdated)
    assert updated and updated[-1].info.autonomous is False
    assert h.store.conversations[updated[-1].info.id].info.autonomous is False


async def test_set_autonomous_applies_from_the_next_turn(make: Any) -> None:
    core = ScriptedCore([Step(text="one"), Step(text="two")])
    h: Harness = await make(core=core)
    await h.start()
    await h.turn("first")
    await h.runtime.set_autonomous(True)
    await h.turn("second")
    assert "idle" not in {t.name for t in core.requests[0].tools}
    assert "idle" in {t.name for t in core.requests[1].tools}
    assert any(d.get("decision") == "set_autonomous" for d in await decisions(h))


async def test_turning_autonomy_on_while_idle_wakes_when_running_work_finishes(
        make: Any) -> None:
    core = ScriptedCore([Step(calls=[call("bg", id="bg")]), Step(text="started"),
                         Step(text="saw the result")])
    tools = FakeTools()
    tools.gated("bg")
    h: Harness = await make(core=core, tools=tools)
    await h.start()
    await h.turn("go")  # bg is backgrounded and still running; the agent is idle
    assert h.runtime.snapshot().operations
    await h.runtime.set_autonomous(True)
    tools.gates["bg"].set()
    await eventually(lambda: len(core.requests) == 3)
    assert {o.call_id for o in op_results(core.requests[2].messages)} == {"bg"}


async def test_set_autonomous_needs_a_started_agent(make: Any) -> None:
    h: Harness = await make()
    with pytest.raises(RuntimeStateError):
        await h.runtime.set_autonomous(True)


async def test_restarted_autonomous_conversation_opens_idle_and_waits(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("work")
    core = ScriptedCore([Step(calls=[call("work", id="w")]), Step(calls=[call("idle")])])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=0.05)
    conv = await h.start(autonomous=True)
    await h.runtime.send("start the work")
    await eventually(lambda: len(core.requests) == 2
                     and h.runtime.snapshot().state == AgentState.IDLE)
    store = h.store
    await h.close()

    h2: Harness = await make(store=store, data_dir=h.data_dir,
                             core=ScriptedCore(default=Step(text="resumed")))
    resumed = await h2.start(conv.id)
    assert resumed.autonomous
    assert h2.runtime.snapshot().state == AgentState.IDLE
    await asyncio.sleep(0.3)
    assert h2.core.requests == []
    await h2.turn("carry on")
    assert len(h2.core.requests) == 1
    assert "idle" in {t.name for t in h2.core.requests[0].tools}


async def test_start_keeps_a_resumed_conversations_switch_unless_set(make: Any) -> None:
    h: Harness = await make()
    conv = await h.start(autonomous=True)
    assert (await h.start(conv.id)).autonomous
    off = await h.start(conv.id, autonomous=False)
    assert not off.autonomous and not h.store.conversations[conv.id].info.autonomous


# ── continue from ──


async def test_seeded_conversation_starts_from_the_sources_summary(make: Any) -> None:
    h: Harness = await make(core=ScriptedCore(default=Step(text="ok")))
    source = await h.start()
    await h.turn("the harbour code is 7731")
    h.store.conversations[source.id].summary = "Earlier: planning the lighthouse trip."
    before = list(await h.runtime.history(source.id))

    seeded = await h.start(seed_from=source.id)
    assert seeded.id != source.id and seeded.seed_from == source.id
    assert await h.transcript() == []
    await h.turn("where were we?")
    system = h.core.requests[-1].system
    assert "planning the lighthouse trip" in system and "7731" in system
    assert list(await h.runtime.history(source.id)) == before
    assert h.store.conversations[source.id].summary == "Earlier: planning the lighthouse trip."


async def test_seed_from_must_exist_and_start_a_new_conversation(make: Any) -> None:
    h: Harness = await make()
    conv = await h.start()
    with pytest.raises(KeyError):
        await h.start(seed_from="missing")
    with pytest.raises(ValueError):
        await h.runtime.start(StartOptions(conv.id, seed_from=conv.id))


# ── long-term memory ──


async def test_conversations_use_and_add_long_term_memory(make: Any) -> None:
    core = ScriptedCore(default=lambda r: Step(text="summary" if r.maintenance else "ok"))
    h: Harness = await make(core=core, memory_kw={"compact_at": 0.002})
    h.memory.extract = ["The user likes tea."]
    conv = await h.start()
    for i in range(3):
        await h.turn(f"message {i} " + "x" * 400)
    entries = await h.runtime.memories()
    assert [(e.text, e.source_conversation_id) for e in entries] == [
        ("The user likes tea.", conv.id)]
    await h.turn("again")
    assert "The user likes tea." in core.requests[-1].system


async def test_recall_all_searches_other_conversations_but_not_archived(make: Any) -> None:
    h: Harness = await make(core=ScriptedCore(default=Step(text="ok")))
    shared = await h.start()
    await h.turn("the falcon launches at dawn")
    archived = await h.start()
    await h.turn("the falcon nests in spring")
    await h.runtime.archive_conversation(archived.id, True)

    h.core.add(*[
        Step(calls=[call("recall", {"query": "falcon"}, id="r1"),
                    call("recall", {"query": "falcon", "scope": "all"}, id="r2")]),
        Step(text="done"),
    ])
    await h.start()
    await h.turn("what about the falcon?")
    results = await h.results()
    assert "dawn" not in results["r1"].content  # this conversation only
    assert results["r2"].status == ToolStatus.OK
    assert "dawn" in results["r2"].content
    assert "spring" not in results["r2"].content
    assert shared.id in results["r2"].content


# ── editable long-term memory ──


async def test_add_edit_forget_change_the_next_context(make: Any) -> None:
    core = ScriptedCore(default=Step(text="ok"))
    h: Harness = await make(core=core)
    await h.start()
    entry = await h.runtime.add_memory("The user lives in Oslo.")
    await h.turn("hi")
    assert "Oslo" in core.requests[-1].system

    edited = await h.runtime.edit_memory(entry.id, "The user lives in Bergen.")
    assert edited.id == entry.id and edited.text == "The user lives in Bergen."
    assert [e.text for e in h.store.long_term[entry.id]] == [
        "The user lives in Oslo.", "The user lives in Bergen."]
    await h.turn("hi again")
    assert "Bergen" in core.requests[-1].system and "Oslo" not in core.requests[-1].system

    await h.runtime.forget_memory(entry.id)
    assert await h.runtime.memories() == []
    await h.turn("and again")
    assert "Bergen" not in core.requests[-1].system
    await settled(h)
    assert len(h.rec.of(MemoriesChanged)) == 3
    logged = [d for d in await decisions(h) if d.get("decision", "").startswith("memory_")]
    assert [d["decision"] for d in logged] == ["memory_added", "memory_edited", "memory_forgotten"]
    assert "Oslo" not in json.dumps(logged) and "Bergen" not in json.dumps(logged)


async def test_forgotten_fact_is_not_re_extracted(make: Any) -> None:
    core = ScriptedCore(default=lambda r: Step(text="summary" if r.maintenance else "ok"))
    h: Harness = await make(core=core, memory_kw={"compact_at": 0.002})
    h.memory.extract = ["The user likes tea."]
    await h.start()
    for i in range(3):
        await h.turn(f"message {i} " + "x" * 400)
    [entry] = await h.runtime.memories()
    await h.runtime.forget_memory(entry.id)
    for i in range(3):
        await h.turn(f"more {i} " + "y" * 400)
    assert await h.runtime.memories() == []


async def test_memory_commands_report_an_unsupported_module(make: Any) -> None:
    h: Harness = await make(memory_kw={"editable": False})
    with pytest.raises(NotImplementedError, match="cannot edit memories"):
        await h.runtime.memories()
    with pytest.raises(NotImplementedError, match="cannot edit memories"):
        await h.runtime.add_memory("x")
    conv = await h.start()
    await h.start()
    with pytest.raises(NotImplementedError, match="cannot delete conversations"):
        await h.runtime.delete_conversation(conv.id)


# ── titles, rename, archive ──


def titling_core(title: str | Exception = "Lighthouse trip") -> ScriptedCore:
    def step(request: InferenceRequest) -> Step:
        if request.maintenance:
            if isinstance(title, Exception):
                return Step(error=title)
            return Step(text=title)
        return Step(text="ok")

    return ScriptedCore(default=step)


async def test_automatic_title_is_written_once(make: Any) -> None:
    h: Harness = await make(core=titling_core(), memory_kw={"auto_title": True})
    conv = await h.start()
    assert conv.title is None
    await h.turn("let's plan the lighthouse trip")
    await h.turn("and the ferry")
    assert h.runtime.snapshot().conversation.title == "Lighthouse trip"
    await settled(h)
    assert len([r for r in h.core.requests if r.maintenance]) == 1
    assert [e.info.title for e in h.rec.of(ConversationUpdated)] == ["Lighthouse trip"]


async def test_users_title_is_never_overwritten(make: Any) -> None:
    h: Harness = await make(core=titling_core(), memory_kw={"auto_title": True})
    conv = await h.start()
    await h.runtime.rename_conversation(conv.id, "My trip")
    await h.turn("let's plan the lighthouse trip")
    assert h.runtime.snapshot().conversation.title == "My trip"
    assert not [r for r in h.core.requests if r.maintenance]


async def test_failed_title_call_changes_nothing(make: Any) -> None:
    from kayden.api import CoreError

    h: Harness = await make(core=titling_core(CoreError("down")),
                            memory_kw={"auto_title": True})
    await h.start()
    await h.turn("hello")
    assert h.runtime.snapshot().conversation.title is None
    assert not h.rec.of(ConversationUpdated)


async def test_rename_and_clear_a_title(make: Any) -> None:
    h: Harness = await make()
    first = await h.start()
    await h.start()
    await h.runtime.rename_conversation(first.id, "Groceries")
    [listed] = [c for c in await h.runtime.list_conversations() if c.id == first.id]
    assert listed.title == "Groceries"
    await h.runtime.rename_conversation(first.id, "   ")  # whitespace only counts as empty
    [listed] = [c for c in await h.runtime.list_conversations() if c.id == first.id]
    assert listed.title is None
    await settled(h)
    assert [e.info.title for e in h.rec.of(ConversationUpdated)] == ["Groceries", None]


async def test_archived_conversations_leave_the_list(make: Any) -> None:
    h: Harness = await make()
    old = await h.start()
    current = await h.start()
    await h.runtime.archive_conversation(old.id, True)
    assert [c.id for c in await h.runtime.list_conversations()] == [current.id]
    assert old.id in [c.id for c in await h.runtime.list_conversations(include_archived=True)]
    await h.runtime.archive_conversation(current.id, True)  # the open one stays open
    assert h.runtime.snapshot().conversation.archived
    await h.runtime.archive_conversation(old.id, False)
    assert [c.id for c in await h.runtime.list_conversations()] == [old.id]


# ── delete ──


async def test_delete_conversation(make: Any) -> None:
    h: Harness = await make(core=ScriptedCore(default=Step(text="ok")))
    doomed = await h.start()
    await h.turn("my password is hunter2")
    entry = h.store.add("The user's password is hunter2.", doomed.id)
    seeded = await h.start(seed_from=doomed.id)
    await h.runtime.delete_conversation(doomed.id)
    assert doomed.id not in h.store.conversations
    assert entry.id not in h.store.long_term
    assert h.store.conversations[seeded.id].info.seed_from is None
    await settled(h)
    assert h.rec.of(ConversationDeleted) == [ConversationDeleted(doomed.id)]
    assert await h.runtime.search_archive("hunter2") == []
    with pytest.raises(KeyError):
        await h.runtime.history(doomed.id)
    logged = [d for d in await decisions(h) if d.get("decision") == "delete_conversation"]
    assert logged == [{**logged[0], "conversation": doomed.id}]


async def test_deleting_the_open_conversation_is_refused(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("work")
    core = ScriptedCore([Step(calls=[call("work", id="w")]), Step(text="waiting")])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=0.05)
    conv = await h.start()
    await h.turn("work")
    assert h.runtime.snapshot().operations
    with pytest.raises(RuntimeStateError):
        await h.runtime.delete_conversation(conv.id)
    assert conv.id in h.store.conversations


# ── per data folder ──


async def test_usage_lock_and_log_are_per_data_folder(make: Any, tmp_path: Path) -> None:
    data = tmp_path / "shared"
    data.mkdir()
    h: Harness = await make(data_dir=data)
    await h.start()
    assert (data / "instance.lock").exists() and (data / "usage.json").exists()

    other: Harness = await make(data_dir=data)
    with pytest.raises(RuntimeError, match="in use"):
        await other.start()


