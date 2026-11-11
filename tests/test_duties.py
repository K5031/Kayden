"""Duties: a person gives a conversation standing work, which the agent plans with
update_plan and continues by itself, woken by finished work, due plan items and a
maximum interval, until it is paused or ended."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any

import pytest

from kayden.api import (
    AgentState,
    Budget,
    ConversationUpdated,
    Duty,
    DutyKind,
    DutyScheduled,
    Failure,
    FailureKind,
    HelpNeeded,
    PlanItem,
    PlanStatus,
    RuntimeStateError,
    Text,
    ToolStatus,
)
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools

LIMIT = Budget(max_cost_usd=5.0)
QUIET = Duty("Keep the inbox answered", min_interval_s=0.05, max_interval_s=30)


async def duty_harness(make: Any, steps: list[Step] | None = None,
                       tools: FakeTools | None = None, **config: Any) -> Harness:
    h: Harness = await make(core=ScriptedCore(steps or []), tools=tools,
                            budget=config.pop("budget", LIMIT), **config)
    await h.start()
    return h


def wakes(h: Harness) -> list[str]:
    """Why each cycle was requested, from the event log."""
    lines = (h.data_dir / "events.jsonl").read_text().splitlines()
    return [e["reason"] for e in map(json.loads, lines) if e.get("decision") == "cycle_requested"]


def notices(request: Any) -> list[str]:
    return [b.text for m in request.messages for b in m.blocks
            if isinstance(b, Text) and "[Runtime notice]" in b.text]


async def idle(h: Harness, requests: int) -> None:
    await eventually(lambda: len(h.core.requests) >= requests
                     and h.runtime.snapshot().state == AgentState.IDLE)


# ── Turning it on and off ──


async def test_a_duty_needs_a_spending_limit(make):
    h = await duty_harness(make, budget=Budget())
    with pytest.raises(ValueError, match="spending limit"):
        await h.runtime.set_duty(QUIET)
    assert h.runtime.snapshot().conversation.duty is None


@pytest.mark.parametrize("duty", [
    Duty("  "), Duty("x", min_interval_s=0), Duty("x", min_interval_s=10, max_interval_s=5)])
async def test_invalid_duties_are_refused(make, duty):
    h = await duty_harness(make)
    with pytest.raises(ValueError):
        await h.runtime.set_duty(duty)


async def test_set_duty_needs_an_open_conversation(make):
    h: Harness = await make(budget=LIMIT)
    with pytest.raises(RuntimeStateError):
        await h.runtime.set_duty(QUIET)


async def test_turning_a_duty_on_starts_work_and_shows_it(make):
    h = await duty_harness(make, [Step(text="on it")])
    await h.runtime.set_duty(QUIET)
    await idle(h, 1)
    [request] = h.core.requests
    assert "Keep the inbox answered" in request.system
    assert "update_plan" in [t.name for t in request.tools]
    assert "finish_goal" not in [t.name for t in request.tools]  # a duty, not a goal
    assert "the duty was turned on" in notices(request)[0]
    [updated] = [e for e in h.rec.of(ConversationUpdated) if e.info.duty is not None]
    assert updated.info.duty == QUIET
    assert h.store.conversations[h.conversation.id].info.duty == QUIET


async def test_an_ordinary_conversation_has_no_duty_tools(make):
    h = await duty_harness(make)
    await h.turn("hi")
    assert {"update_plan", "finish_goal"}.isdisjoint(t.name for t in h.core.requests[0].tools)


async def test_a_duty_can_be_added_mid_conversation_and_keeps_the_context(make):
    h = await duty_harness(make, [Step(text="noted"), Step(text="continuing")])
    await h.turn("we sell bikes")
    await h.runtime.set_duty(QUIET)
    await idle(h, 2)
    assert any("we sell bikes" in str(m.blocks) for m in h.core.requests[1].messages)


async def test_turning_it_off_ends_the_schedule_and_keeps_the_plan(make):
    h = await duty_harness(make, [Step(calls=[call("update_plan", {
        "items": [{"text": "Read mail"}]})]), Step(text="planned")])
    await h.runtime.set_duty(QUIET)
    await idle(h, 2)
    assert h.runtime.snapshot().duty_wake_at is not None
    await h.runtime.set_duty(None)
    info = h.runtime.snapshot().conversation
    assert info.duty is None and [i.text for i in info.plan] == ["Read mail"]
    assert h.runtime.snapshot().duty_wake_at is None
    await h.rec.wait_for(lambda r: r.of(DutyScheduled)[-1] == DutyScheduled(None))
    await h.turn("thanks")
    request = h.core.requests[-1]
    assert "Read mail" in request.system  # the plan is still shown
    assert "update_plan" in [t.name for t in request.tools]


# ── The plan ──


async def test_update_plan_saves_the_plan_and_every_request_shows_it(make):
    h = await duty_harness(make, [
        Step(calls=[call("update_plan", {"items": [
            {"text": "Read mail", "status": "doing", "note": "start with VIPs"},
            {"text": "Weekly report", "due": "2030-01-07T09:00"},
            {"text": "Call back", "due_in_minutes": 90}]})]),
        Step(text="planned")])
    before = time.time()
    await h.runtime.set_duty(QUIET)
    await idle(h, 2)
    plan = h.runtime.snapshot().conversation.plan
    assert [(i.text, i.status) for i in plan] == [
        ("Read mail", PlanStatus.DOING), ("Weekly report", PlanStatus.TODO),
        ("Call back", PlanStatus.TODO)]
    assert plan[0].note == "start with VIPs" and plan[0].due is None
    assert plan[1].due == pytest.approx(
        datetime(2030, 1, 7, 9).astimezone().timestamp())
    assert before + 90 * 60 <= plan[2].due <= time.time() + 90 * 60
    assert "Weekly report" in h.core.requests[1].system
    assert h.store.conversations[h.conversation.id].info.plan == plan


async def test_a_bad_due_time_is_an_error_and_changes_nothing(make):
    h = await duty_harness(make, [
        Step(calls=[call("update_plan", {"items": [{"text": "x", "due": "soon"}]}, id="p")]),
        Step(text="oops")])
    await h.runtime.set_duty(QUIET)
    await idle(h, 2)
    assert (await h.results())["p"].status == ToolStatus.ERROR
    assert h.runtime.snapshot().conversation.plan == ()


async def test_the_user_edits_the_plan(make):
    h = await duty_harness(make)
    await h.runtime.set_plan([PlanItem("Ship it", due=2e9)])
    assert [i.text for i in h.runtime.snapshot().conversation.plan] == ["Ship it"]
    await h.rec.wait_for(lambda r: any(e.info.plan and e.info.plan[0].due == 2e9
                                       for e in r.of(ConversationUpdated)))


# ── Waking up ──


async def test_with_nothing_due_it_wakes_at_the_maximum_interval(make):
    h = await duty_harness(make, [Step(text="checked"), Step(text="checked again")])
    await h.runtime.set_duty(Duty("Watch", min_interval_s=0.05, max_interval_s=0.3))
    await idle(h, 1)
    wake_at = h.runtime.snapshot().duty_wake_at
    assert wake_at is not None and wake_at - time.time() <= 0.3
    await idle(h, 2)
    assert "scheduled check-in" in notices(h.core.requests[1])[-1]
    await eventually(lambda: "duty: scheduled check-in" in wakes(h))  # logged with its cause


async def test_a_due_plan_item_wakes_it_early(make):
    h = await duty_harness(make, [
        Step(calls=[call("update_plan", {"items": [{"text": "Post", "due_in_minutes": 0.003}]})]),
        Step(text="later"), Step(text="posting")])
    await h.runtime.set_duty(Duty("Run socials", min_interval_s=0.05, max_interval_s=30))
    await idle(h, 3)
    assert "a plan item is due" in notices(h.core.requests[2])[-1]


async def test_the_minimum_interval_holds_back_an_overdue_item(make):
    h = await duty_harness(make, [Step(text="done")])
    await h.runtime.set_plan([PlanItem("Overdue", due=time.time() - 60)])
    await h.runtime.set_duty(Duty("x", min_interval_s=5, max_interval_s=30))
    await idle(h, 1)
    assert h.runtime.snapshot().duty_wake_at - time.time() > 4


async def test_it_wakes_on_the_first_finished_operation(make):
    tools = FakeTools()
    tools.slow("watch_mail", 0.3)
    tools.slow("long_build", 30)
    h = await duty_harness(make, [
        Step(calls=[call("watch_mail", id="w"), call("long_build", id="b")]),
        Step(text="waiting"), Step(text="mail came in")], tools=tools)
    await h.runtime.set_duty(QUIET)
    await idle(h, 3)
    assert "b" in [op.call.id for op in h.runtime.snapshot().operations]  # still running
    assert "background work finished" in notices(h.core.requests[2])[-1]


async def test_a_user_message_still_wakes_it(make):
    h = await duty_harness(make, [Step(text="on it"), Step(text="sure")])
    await h.runtime.set_duty(QUIET)
    await idle(h, 1)
    await h.turn("also check spam")
    assert notices(h.core.requests[1]) == notices(h.core.requests[0])  # no new notice


async def test_editing_the_duty_while_idle_reschedules_without_starting_work(make):
    h = await duty_harness(make, [Step(text="on it")])
    await h.runtime.set_duty(QUIET)
    await idle(h, 1)
    await h.runtime.set_duty(Duty("Keep it answered", min_interval_s=0.05, max_interval_s=600))
    assert h.runtime.snapshot().duty_wake_at - time.time() > 500
    await asyncio.sleep(0.1)
    assert len(h.core.requests) == 1


# ── Pausing ──


async def test_stop_pauses_and_activate_resumes(make):
    h = await duty_harness(make, [Step(text="on it"), Step(text="back")])
    await h.runtime.set_duty(Duty("Watch", min_interval_s=0.05, max_interval_s=0.2))
    await idle(h, 1)
    await h.runtime.stop()  # nothing outstanding: no full Stop, but the schedule ends
    assert h.runtime.snapshot().duty_wake_at is None
    await asyncio.sleep(0.4)
    assert len(h.core.requests) == 1
    await h.runtime.activate()
    await idle(h, 2)
    assert h.runtime.snapshot().duty_wake_at is not None


async def test_a_restart_leaves_the_duty_paused(make):
    h = await duty_harness(make, [Step(text="on it")])
    await h.runtime.set_duty(Duty("Watch", min_interval_s=0.05, max_interval_s=0.2))
    await idle(h, 1)
    conversation = h.conversation.id
    await h.close()
    again: Harness = await make(core=ScriptedCore(), store=h.store, budget=LIMIT)
    info = await again.start(conversation)
    assert info.duty is not None
    await asyncio.sleep(0.4)
    assert again.core.requests == [] and again.runtime.snapshot().duty_wake_at is None


async def test_an_exhausted_budget_pauses_it(make):
    h = await duty_harness(make, [Step(text="expensive")], budget=Budget(max_cost_usd=1e-9))
    await h.runtime.set_duty(Duty("Watch", min_interval_s=0.05, max_interval_s=0.1))
    await eventually(lambda: h.runtime.snapshot().state == AgentState.FAILED)
    await asyncio.sleep(0.3)
    assert len(h.core.requests) == 1 and h.runtime.snapshot().duty_wake_at is None


async def test_without_a_spending_limit_it_pauses_and_says_so(make):
    h = await duty_harness(make, [Step(text="on it")])
    await h.runtime.set_duty(QUIET)
    await idle(h, 1)
    conversation = h.conversation.id
    await h.close()
    again: Harness = await make(core=ScriptedCore([Step(text="back")]), store=h.store)
    await again.start(conversation)  # the limit is gone in this instance
    await again.runtime.activate()
    await eventually(lambda: any(f.kind == FailureKind.BUDGET and "spending limit" in f.message
                                 for f in again.rec.of(Failure)))
    assert len(again.core.requests) == 1 and again.runtime.snapshot().duty_wake_at is None


# ── Goals ──


async def test_a_goal_ends_when_the_agent_declares_it_reached(make):
    h = await duty_harness(make, [
        Step(calls=[call("update_plan", {"items": [{"text": "Launch", "status": "done"}]})]),
        Step(calls=[call("finish_goal", {"report": "Launched on Monday."}, id="f")]),
        Step(text="All done.")])
    await h.runtime.set_duty(Duty("Launch the campaign", DutyKind.GOAL,
                                  min_interval_s=0.05, max_interval_s=0.1))
    await idle(h, 3)
    assert "finish_goal" in [t.name for t in h.core.requests[0].tools]
    assert (await h.results())["f"].status == ToolStatus.OK
    info = h.runtime.snapshot().conversation
    assert info.duty is None and [i.text for i in info.plan] == ["Launch"]
    await asyncio.sleep(0.3)
    assert len(h.core.requests) == 3 and h.runtime.snapshot().duty_wake_at is None


async def test_continue_from_does_not_copy_the_duty(make):
    h = await duty_harness(make, [Step(text="on it")])
    await h.runtime.set_duty(QUIET)
    await idle(h, 1)
    source = h.conversation.id
    seeded = await h.start(seed_from=source)
    assert seeded.duty is None and h.runtime.snapshot().duty_wake_at is None


# ── Asking for help ──


async def test_asking_for_help_pauses_the_duty_until_the_user_answers(make):
    h = await duty_harness(make, [
        Step(calls=[call("ask_for_help", {"question": "Which account should I post from?"},
                         id="ask")]),
        Step(text="Waiting for you."), Step(text="Posting from the main account.")])
    await h.runtime.set_duty(Duty("Run socials", min_interval_s=0.05, max_interval_s=0.2))
    await idle(h, 2)
    assert "ask_for_help" in [t.name for t in h.core.requests[0].tools]
    assert (await h.results())["ask"].status == ToolStatus.OK
    await h.rec.wait_for(lambda r: HelpNeeded("Which account should I post from?")
                         in r.of(HelpNeeded))
    snap = h.runtime.snapshot()
    assert snap.help_needed == "Which account should I post from?"
    assert snap.duty_wake_at is None
    await asyncio.sleep(0.4)
    assert len(h.core.requests) == 2  # no check-ins while it waits
    await h.turn("The main one.")
    await h.rec.wait_for(lambda r: r.of(HelpNeeded)[-1] == HelpNeeded(None))
    assert h.runtime.snapshot().help_needed is None
    assert h.runtime.snapshot().duty_wake_at is not None  # the schedule is back


async def test_finished_work_still_wakes_an_agent_waiting_for_help(make):
    tools = FakeTools()
    tools.slow("watch", 0.3)
    h = await duty_harness(make, [
        Step(calls=[call("watch", id="w"),
                    call("ask_for_help", {"question": "Login code?"})]),
        Step(text="Waiting."), Step(text="The watcher fired.")], tools=tools)
    await h.runtime.set_duty(QUIET)
    await idle(h, 3)
    assert "background work finished" in notices(h.core.requests[2])[-1]
    assert h.runtime.snapshot().help_needed == "Login code?"  # still unanswered


async def test_ask_for_help_needs_a_duty(make):
    h = await duty_harness(make)
    await h.runtime.set_plan([PlanItem("x")])  # a plan alone offers update_plan only
    await h.turn("hi")
    assert "ask_for_help" not in [t.name for t in h.core.requests[0].tools]


async def test_the_task_stays_in_view_after_the_goal_is_finished(make):
    h = await duty_harness(make, [
        Step(calls=[call("finish_goal", {"report": "Launched."})]),
        Step(text="All done.")])
    await h.runtime.set_duty(Duty("Launch the campaign", DutyKind.GOAL, min_interval_s=0.05,
                                  max_interval_s=30))
    await idle(h, 2)
    first, after = h.core.requests[0], h.core.requests[1]
    assert "Your goal: Launch the campaign" in notices(first)[0]
    assert "Launch the campaign" not in after.system  # the goal has ended
    assert any("Launch the campaign" in text for text in notices(after))  # still in view
