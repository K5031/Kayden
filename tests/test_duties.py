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

from kadence.api import (
    AgentState,
    Budget,
    ConversationUpdated,
    Duty,
    DutyKind,
    DutyScheduled,
    Failure,
    HelpNeeded,
    PlanItem,
    PlanStatus,
    Role,
    RuntimeStateError,
    Text,
    ToolResult,
    ToolStatus,
)
from kadence.runtime import duty as duties
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


async def test_a_duty_runs_without_a_spending_limit(make):
    """A limit is the user's choice, as in Claude's apps: nothing requires one."""
    h = await duty_harness(make, [Step(text="on it")], budget=Budget())
    await h.runtime.set_duty(QUIET)
    await idle(h, 1)
    assert h.runtime.snapshot().conversation.duty is not None
    assert h.runtime.snapshot().duty_wake_at is not None  # and it checks in again
    assert not h.rec.of(Failure)


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
    assert notices(request)[0].endswith("[Runtime notice] New duty: Keep the inbox answered")
    [updated] = [e for e in h.rec.of(ConversationUpdated) if e.info.duty is not None]
    assert updated.info.duty == QUIET
    assert h.store.conversations[h.conversation.id].info.duty == QUIET


async def test_an_ordinary_conversation_keeps_a_plan_but_has_no_duty_tools(make):
    h = await duty_harness(make, [
        Step(calls=[call("update_plan", {"items": [
            {"text": "Read the code", "status": "done"},
            {"text": "Fix the bug", "status": "doing"},
            {"text": "Run the tests"}]}, id="p")]),
        Step(text="Working on it.")])
    await h.turn("fix the bug")
    names = {t.name for t in h.core.requests[0].tools}
    assert "update_plan" in names  # a plan for any task with steps
    assert names.isdisjoint({"finish_goal", "ask_for_help"})
    plan = h.runtime.snapshot().conversation.plan
    assert [(i.text, i.status) for i in plan] == [
        ("Read the code", PlanStatus.DONE), ("Fix the bug", PlanStatus.DOING),
        ("Run the tests", PlanStatus.TODO)]
    assert h.runtime.snapshot().conversation.duty is None
    # The agent's own call shows the plan from here on: the prompt does not repeat it,
    # so a plan update leaves the start of every later request as it was.
    assert h.core.requests[1].system == h.core.requests[0].system
    assert "Fix the bug" not in h.core.requests[1].system
    result = (await h.results())["p"].content
    assert "Plan saved" in result


async def test_the_prompt_shows_a_plan_the_conversation_does_not(make):
    """A plan the user edited, or one whose update_plan call is no longer in view (a
    conversation opened again, older messages summarized), is in the prompt."""
    h = await duty_harness(make, [
        Step(calls=[call("update_plan", {"items": [
            {"text": "Fix the bug", "status": "done"}]}, id="p")]),
        Step(text="Fixed."), Step(text="Sure."),
        Step(calls=[call("update_plan", {"items": [
            {"text": "Ship it", "status": "done"}]}, id="q")]),
        Step(text="Shipped."), Step(text="Hello again.")])
    await h.turn("fix the bug")
    await h.runtime.set_plan([PlanItem("Write the docs first")])
    await h.turn("carry on")
    system = h.core.requests[2].system
    assert system.endswith("## Plan\n- [todo] Write the docs first")
    await h.turn("plan again")  # the agent's own update is in the conversation again
    assert "## Plan" not in h.core.requests[4].system
    first = h.conversation.id
    await h.chat("Other")  # away and back: what the conversation shows is not known
    await h.start(conversation_id=first)
    await h.turn("hello again")
    assert "- [done] Ship it" in h.core.requests[5].system


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
    # The plan is still in view: the agent's own call shows it.
    assert any(getattr(b, "name", None) == "update_plan" and b.arguments["items"]
               for m in request.messages for b in m.blocks)
    assert "update_plan" in [t.name for t in request.tools]


# ── The plan ──


# ── Reminders: a due step without a duty ──


def soon(text: str = "Stand up") -> Step:
    return Step(calls=[call("update_plan", {"items": [
        {"text": text, "due_in_minutes": 0.002}]}, id="p")])  # about a tenth of a second


async def test_a_due_step_wakes_the_agent_without_a_duty(make):
    h = await duty_harness(make, [soon(), Step(text="I will remind you."),
                                  Step(text="Time to stand up!")], reminder_min_s=0.05,
                           budget=Budget())  # no spending limit is needed
    await h.turn("remind me to stand up in a moment")
    assert "woken" in (await h.results())["p"].content
    assert h.runtime.snapshot().duty_wake_at is not None  # frontends show when
    await idle(h, 3)
    assert wakes(h)[-1] == "duty: a plan item is due"
    assert "[Runtime notice] Due now in your plan: Stand up." in notices(
        h.core.requests[2])[-1]
    # The step is still open and overdue: it reminded once, not again and again.
    await asyncio.sleep(0.3)
    assert len(h.core.requests) == 3 and h.runtime.snapshot().duty_wake_at is None


async def test_a_reopened_conversation_reminds_of_what_is_still_to_come_only(make):
    """A step that fell due while the conversation was closed waits for the user, as a
    duty does after a restart; one that is still ahead wakes the agent."""
    h = await duty_harness(make, [Step(text="Second one!")], reminder_min_s=0.05)
    now = time.time()
    await h.runtime.set_plan([PlanItem("First", due=now - 60),
                              PlanItem("Second", due=now + 1.0)])
    first = h.conversation.id
    await h.chat("Other")
    await h.start(conversation_id=first)
    await idle(h, 1)
    assert "[Runtime notice] Due now in your plan: First; Second." in notices(
        h.core.requests[0])[-1]


async def test_stop_puts_reminders_off_until_the_next_message(make):
    h = await duty_harness(make, [soon(), Step(text="ok", hang=True), Step(text="Back."),
                                  Step(text="Stand up!")], reminder_min_s=0.05)
    await h.runtime.send("remind me")
    await eventually(lambda: len(h.core.requests) == 2)
    await h.runtime.stop()
    await eventually(lambda: h.runtime.snapshot().state == AgentState.STOPPED)
    await asyncio.sleep(0.3)
    assert len(h.core.requests) == 2  # stopped: nothing wakes it
    await h.turn("carry on")
    await idle(h, 4)
    assert wakes(h)[-1] == "duty: a plan item is due"


# ── Finishing the plan ──


def plan_call(*items: tuple[str, str], id: str = "p") -> Step:
    return Step(calls=[call("update_plan", {"items": [
        {"text": text, "status": status} for text, status in items]}, id=id)])


async def test_a_turn_may_end_with_steps_open(make):
    """Whether its plan is finished is the agent's to judge: the runtime neither asks it
    again nor writes into the conversation."""
    h = await duty_harness(make, [
        plan_call(("Read the code", "done"), ("Fix the bug", "doing")),
        Step(text="I looked at it.")])
    await h.turn("fix the bug")
    await asyncio.sleep(0.2)
    assert len(h.core.requests) == 2
    assert h.runtime.snapshot().state == AgentState.IDLE
    assert h.runtime.snapshot().conversation.plan[1].status == PlanStatus.DOING
    assert [m.role for m in await h.transcript()] == [
        Role.USER, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT]


def test_what_update_plan_says_about_due_steps():
    later = PlanItem("Check the build", PlanStatus.TODO, due=time.time() + 600)
    now = time.time()
    assert duties.WAKES in duties.saved([later], None, now)
    assert duties.WAKES not in duties.saved([later], Duty("Watch the build"), now)
    assert duties.WAKES not in duties.saved([PlanItem("Fix it")], None, now)


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
    assert "Weekly report" not in h.core.requests[1].system  # the result shows it
    result = next(b for m in h.core.requests[1].messages for b in m.blocks
                  if isinstance(b, ToolResult))
    assert "- Weekly report: due 2030-01-07T09:00" in result.content
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
    assert "Time to check in on your duty." in notices(h.core.requests[1])[-1]
    await eventually(lambda: "duty: scheduled check-in" in wakes(h))  # logged with its cause


async def test_a_due_plan_item_wakes_it_early(make):
    h = await duty_harness(make, [
        Step(calls=[call("update_plan", {"items": [{"text": "Post", "due_in_minutes": 0.003}]})]),
        Step(text="later"), Step(text="posting")])
    await h.runtime.set_duty(Duty("Run socials", min_interval_s=0.05, max_interval_s=30))
    await idle(h, 3)
    assert "A step in your plan is due." in notices(h.core.requests[2])[-1]


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
    assert "Background work finished." in notices(h.core.requests[2])[-1]


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
    assert "Background work finished." in notices(h.core.requests[2])[-1]
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
    assert "New goal: Launch the campaign" in notices(first)[0]
    assert "Launch the campaign" not in after.system  # the goal has ended
    assert any("Launch the campaign" in text for text in notices(after))  # still in view


async def test_the_terminal_app_shows_the_plan_as_a_checklist(make):
    from kadence.frontends.tui import KadenceApp, PlanPanel
    from tests.test_tui import type_text, until

    h = await duty_harness(make, [
        Step(calls=[call("update_plan", {"items": [
            {"text": "Read the code", "status": "done"},
            {"text": "Fix the bug", "status": "doing"},
            {"text": "Run the tests"}]})]),
        Step(text="Working on it.", hang=True)])  # still working: the checklist shows
    app = KadenceApp(h.runtime)
    async with app.run_test(size=(100, 40)) as pilot:
        await until(pilot, lambda: app.conversation is not None)
        await type_text(pilot, "fix the bug")
        await pilot.press("enter")
        await until(pilot, lambda: app.query(PlanPanel), timeout=10)
        assert app.query_one(PlanPanel).lines() == [
            "✓ Read the code", "◐ Fix the bug", "○ Run the tests"]
        await h.runtime.stop()  # no longer working: it goes, though a step is open
        await until(pilot, lambda: not app.query(PlanPanel), timeout=10)


def test_the_agent_is_told_to_plan_first_whenever_a_task_is_more_than_one_action():
    """The rule is in the tool's description, which reaches the model whatever system
    prompt the user has written."""
    from kadence.api import DUTY_TOOLS

    told = DUTY_TOOLS["update_plan"].description
    assert "Every task that edits files, runs more than one command or has several steps" in told
    assert "with your very first tool call" in told


async def test_finishing_a_goal_finishes_the_step_the_agent_was_on(make):
    """The step in progress when the agent declares the goal reached is the one that
    call completes (often "Report"). A step it never began stays open."""
    plan = [{"text": "Launch", "status": "done"}, {"text": "Report", "status": "doing"}]
    h = await duty_harness(make, [
        Step(calls=[call("update_plan", {"items": plan}),
                    call("finish_goal", {"report": "Launched."})]),
        Step(text="All done.")])
    await h.runtime.set_duty(Duty("Launch the campaign", DutyKind.GOAL, min_interval_s=0.05,
                                  max_interval_s=30))
    await idle(h, 2)
    await asyncio.sleep(0.2)
    assert len(h.core.requests) == 2
    assert [(i.text, i.status) for i in h.runtime.snapshot().conversation.plan] == [
        ("Launch", PlanStatus.DONE), ("Report", PlanStatus.DONE)]

    plan.append({"text": "Tell the press", "status": "todo"})
    h = await duty_harness(make, [
        Step(calls=[call("update_plan", {"items": plan}),
                    call("finish_goal", {"report": "Launched."})]),
        Step(text="All done.")])
    await h.runtime.set_duty(Duty("Launch the campaign", DutyKind.GOAL, min_interval_s=0.05,
                                  max_interval_s=30))
    await idle(h, 2)
    await asyncio.sleep(0.2)
    assert len(h.core.requests) == 2
    assert [i.status for i in h.runtime.snapshot().conversation.plan] == [
        PlanStatus.DONE, PlanStatus.DONE, PlanStatus.TODO]


def test_finish_goal_says_what_it_does_to_the_plan():
    from kadence.api import DUTY_TOOLS

    told = DUTY_TOOLS["finish_goal"].description
    assert "The step you are on counts as done" in told and "stays open" in told
