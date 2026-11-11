"""Waking up after background work, fresh starts, continuity, and conversation and
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
    ToolStatus,
)
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


# ── waking up ──


async def test_a_conversation_wakes_once_after_the_batch(make: Any) -> None:
    h: Harness = await make(core=batch_core(), tools=batch_tools())
    await h.start()
    await h.runtime.send("run the batch")
    await eventually(lambda: len(h.core.requests) == 3)
    assert {o.call_id for o in op_results(h.core.requests[2].messages)} == {"a", "b", "c"}
    await asyncio.sleep(0.2)
    assert len(h.core.requests) == 3
    # The agent's instructions and tools are the same for the whole conversation.
    first, _, third = h.core.requests
    assert first.system == third.system and first.tools == third.tools


async def test_restarted_conversation_opens_idle_and_waits(make: Any) -> None:
    tools = FakeTools()
    tools.hanging("work")
    core = ScriptedCore([Step(calls=[call("work", id="w")]), Step(text="waiting")])
    h: Harness = await make(core=core, tools=tools, initial_wait_s=0.05)
    conv = await h.start()
    await h.runtime.send("start the work")
    await eventually(lambda: len(core.requests) == 2
                     and h.runtime.snapshot().state == AgentState.IDLE)
    store = h.store
    await h.close()

    h2: Harness = await make(store=store, data_dir=h.data_dir,
                             core=ScriptedCore(default=Step(text="resumed")))
    await h2.start(conv.id)
    assert h2.runtime.snapshot().state == AgentState.IDLE
    await asyncio.sleep(0.3)
    assert h2.core.requests == []
    await h2.turn("carry on")
    assert len(h2.core.requests) == 1


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


async def test_memory_the_user_writes_changes_the_next_context(make: Any) -> None:
    core = ScriptedCore(default=Step(text="ok"))
    h: Harness = await make(core=core)
    await h.start()
    await h.runtime.write_memory_file("MEMORY.md", "- [Trip](trip.md) — the spring trip\n")
    await h.runtime.write_memory_file("trip.md", "Flights booked for May.\n")
    await h.turn("hi")
    system = core.requests[-1].system
    assert "- [Trip](trip.md) — the spring trip" in system
    assert "Flights booked" not in system  # a memory file is read on demand

    await h.runtime.write_memory_file("MEMORY.md", "- [Trip](trip.md) — the summer trip\n")
    await h.turn("hi again")
    assert "the summer trip" in core.requests[-1].system

    await h.runtime.delete_memory_file("trip.md")
    await h.runtime.delete_memory_file("MEMORY.md")
    assert await h.runtime.memory_files() == []
    await h.turn("and again")
    assert "Trip" not in core.requests[-1].system
    await settled(h)
    assert len(h.rec.of(MemoriesChanged)) >= 4
    logged = [d for d in await decisions(h) if d.get("decision", "").startswith("memory_")]
    assert [d["decision"] for d in logged] == ["memory_written"] * 3 + ["memory_deleted"] * 2
    assert "summer" not in json.dumps(logged) and "May" not in json.dumps(logged)


async def test_memory_commands_report_an_unsupported_module(make: Any) -> None:
    h: Harness = await make(memory_kw={"editable": False})
    with pytest.raises(NotImplementedError, match="cannot edit memories"):
        await h.runtime.memory_files()
    with pytest.raises(NotImplementedError, match="cannot edit memories"):
        await h.runtime.write_memory_file("x.md", "y")
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
    assert [e.info.title for e in h.rec.of(ConversationUpdated)] == [None, "Lighthouse trip"]


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
    assert [e.info.title for e in h.rec.of(ConversationUpdated)] == [None]  # stored only


async def test_rename_and_clear_a_title(make: Any) -> None:
    h: Harness = await make()
    first = await h.start()
    await h.runtime.rename_conversation(first.id, "Groceries")
    await h.start()
    [listed] = [c for c in await h.runtime.list_conversations() if c.id == first.id]
    assert listed.title == "Groceries"
    await h.runtime.rename_conversation(first.id, "   ")  # whitespace only counts as empty
    [listed] = [c for c in await h.runtime.list_conversations() if c.id == first.id]
    assert listed.title is None
    await settled(h)
    assert [e.info.title for e in h.rec.of(ConversationUpdated)] == [None, "Groceries", None]


async def test_archived_conversations_leave_the_list(make: Any) -> None:
    h: Harness = await make()
    old = await h.start()
    await h.runtime.rename_conversation(old.id, "Old")
    current = await h.start()
    await h.runtime.rename_conversation(current.id, "Current")
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
    await h.runtime.write_memory_file("account.md", "Has an account.\n")
    seeded = await h.start(seed_from=doomed.id)
    await h.runtime.delete_conversation(doomed.id)
    assert doomed.id not in h.store.conversations
    assert [f.path for f in await h.runtime.memory_files()] == ["account.md"]  # the project's
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




# ── a new chat is stored with its first use ──────────────────────────────


async def test_a_new_chat_is_stored_only_with_its_first_message(make):
    h: Harness = await make()
    h.core.add(Step(text="hi"))
    new = await h.start()
    assert await h.runtime.list_conversations() == []
    assert await h.runtime.history(new.id) == []
    assert await h.runtime.inspect_memory(new.id) is None
    assert h.runtime.snapshot().conversation.id == new.id
    await h.turn("hello")
    [stored] = await h.runtime.list_conversations()
    assert stored.id == new.id and stored.preview == "hello"
    assert any(e.info.id == new.id for e in h.rec.of(ConversationUpdated))


async def test_leaving_an_unused_chat_leaves_nothing_behind(make):
    h: Harness = await make()
    await h.start()
    await h.runtime.start(StartOptions())
    await h.runtime.start(StartOptions())
    assert await h.runtime.list_conversations() == []


async def test_naming_a_new_chat_or_giving_it_a_duty_stores_it(make):
    from kayden.api import Budget, Duty

    h: Harness = await make(budget=Budget(max_cost_usd=5.0))
    named = await h.start()
    await h.runtime.rename_conversation(named.id, "Trip")
    assert [c.title for c in await h.runtime.list_conversations()] == ["Trip"]
    dutiful = await h.runtime.start(StartOptions())
    await h.runtime.set_duty(Duty("Watch the inbox", min_interval_s=60, max_interval_s=120))
    assert dutiful.id in [c.id for c in await h.runtime.list_conversations()]


async def test_activating_a_new_chat_stores_it(make):
    h: Harness = await make()
    h.core.add(Step(text="Hello!"))
    new = await h.start()
    await h.runtime.activate()
    await h.rec.wait_for(lambda r: len(h.core.requests) == 1)
    await eventually(lambda: h.runtime.snapshot().state == AgentState.IDLE)
    assert [c.id for c in await h.runtime.list_conversations()] == [new.id]


async def test_switching_conversations_does_not_list_the_models_again(make):
    h: Harness = await make()
    first = await h.chat()
    await h.chat()
    listed = h.core.listed
    await h.start(first.id)
    await h.start()
    assert h.core.listed == listed  # a provider round trip per switch felt as lag
