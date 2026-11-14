"""Duties: the prompt section, the plan the agent writes with update_plan, and when the
duty wakes next."""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from kadence.api import NOTICE_PREFIX, Duty, DutyKind, PlanItem, PlanStatus

TURNED_ON = "the duty was turned on"
DUE = "a plan item is due"
CHECK_IN = "scheduled check-in"
FINISHED = "background work finished"
OPEN = (PlanStatus.TODO, PlanStatus.DOING)


def local(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="minutes")


def _span(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        n, unit = int(seconds // 3600), "hour"
    elif seconds >= 60:
        n, unit = round(seconds / 60), "minute"
    else:
        n, unit = round(seconds), "second"
    return f"{n} {unit}{'s' if n != 1 else ''}"


WAKES = ("You will be woken when a step falls due, as long as this conversation stays open "
         "in Kadence: you can end your turn meanwhile.")


def saved(plan: Sequence[PlanItem], duty: Duty | None, now: float) -> str:
    """What update_plan answers. The call and this answer are how the plan stays in view,
    so it says what the call's arguments do not: the time, when each step is due, and
    that a due step wakes the agent."""
    lines = [f"Plan saved. It is now {local(now)}."]
    due = [f"- {i.text}: due {local(i.due)}" for i in plan if i.due and i.status in OPEN]
    lines += due
    if due and duty is None:
        lines.append(WAKES)
    return "\n".join(lines)


def prompt(duty: Duty | None, plan: Sequence[PlanItem], *, in_view: bool = False) -> str:
    """The system prompt section for a conversation with a duty or a plan. A plan the
    conversation itself shows (`in_view`: the agent's update_plan call is among the
    messages) is left out, so that updating it does not change the prompt."""
    parts = []
    if duty is not None:
        until = (" until the goal is reached; then call finish_goal with a short report"
                 if duty.kind == DutyKind.GOAL else "")
        parts.append(
            f"## Your duty\n{duty.instruction}\n\n"
            f"This is standing work{until}. Carry it on by yourself, without waiting for "
            "the user. Keep a plan with update_plan: the steps, what you are doing, and "
            "when each falls due. After you end your turn you are woken again: when "
            "background work finishes, when an open plan item falls due, and at least "
            f"every {_span(duty.max_interval_s)}. To react to something outside (new mail, "
            "a webhook, a file), start a tool that waits for it; when it finishes it wakes "
            "you. If you are blocked and only your supervisor "
            "can unblock you, call ask_for_help and end your turn; solve what you can "
            "yourself first.")
    if plan and in_view:
        pass
    elif plan:
        lines = [f"- [{i.status}] {i.text}" + (f" (due {local(i.due)})" if i.due else "")
                 + (f" — {i.note}" if i.note else "") for i in plan]
        parts.append("## Plan\n" + "\n".join(lines))
    elif duty is not None:
        parts.append("## Plan\n(empty: write one with update_plan)")
    return "\n\n".join(parts)


_SAYS = {DUE: "A step in your plan is due.", CHECK_IN: "Time to check in on your {kind}.",
         FINISHED: "Background work finished."}


def notice(cause: str, duty: Duty | None) -> str:
    """The message that starts a cycle a duty started. The one that turns it on states the
    task, so the transcript keeps it after the duty ends (a goal reached mid-cycle)."""
    kind = "goal" if duty is not None and duty.kind == DutyKind.GOAL else "duty"
    if cause == TURNED_ON and duty is not None:
        return f"{NOTICE_PREFIX}New {kind}: {duty.instruction}"
    says = _SAYS.get(cause, "{cause}").format(kind=kind, cause=cause)
    return (f"{NOTICE_PREFIX}{says} Carry on with your {kind}, or end your turn if there is "
            "nothing to do now.")


def next_reminder(plan: Sequence[PlanItem], after: float) -> float | None:
    """Without a duty: when the next open step falls due, counting only those due later
    than `after` (the last reminder), so a step left open reminds once."""
    due = [i.due for i in plan if i.due is not None and i.status in OPEN and i.due > after]
    return min(due) if due else None


def reminder(plan: Sequence[PlanItem], now: float) -> str:
    """The message that starts a cycle for a due step in a conversation without a duty."""
    due = "; ".join(i.text for i in plan
                    if i.due is not None and i.status in OPEN and i.due <= now)
    return (f"{NOTICE_PREFIX}Due now in your plan: {due}. This is the time you set: do what "
            "the step says (for a reminder, tell the user), then update the plan.")


def parse_plan(items: Sequence[dict[str, Any]], now: float) -> list[PlanItem]:
    """update_plan's items; ValueError for a due time that cannot be read."""
    plan = []
    for item in items:
        due = None
        if "due_in_minutes" in item:
            due = now + 60 * float(item["due_in_minutes"])
        elif item.get("due"):
            try:
                when = datetime.fromisoformat(item["due"])
            except ValueError:
                raise ValueError(f"Cannot read the due time {item['due']!r}: use ISO 8601, "
                                 "e.g. 2026-11-12T09:00.") from None
            due = (when if when.tzinfo else when.astimezone()).timestamp()
        plan.append(PlanItem(item["text"], PlanStatus(item.get("status", "todo")), due,
                             item.get("note", "")))
    return plan


def next_wake(duty: Duty, plan: Sequence[PlanItem], now: float | None = None
              ) -> tuple[float, str]:
    """(seconds from now, cause): the earliest open item due, held between the duty's
    intervals; with nothing due, the maximum interval."""
    now = time.time() if now is None else now
    due = [i.due for i in plan if i.due is not None and i.status in OPEN]
    if due and min(due) - now < duty.max_interval_s:
        return max(duty.min_interval_s, min(due) - now), DUE
    return duty.max_interval_s, CHECK_IN


def check(duty: Duty) -> Duty:
    """The duty with its instruction trimmed; ValueError when it cannot run."""
    instruction = duty.instruction.strip()
    if not instruction:
        raise ValueError("a duty needs an instruction")
    if not 0 < duty.min_interval_s <= duty.max_interval_s:
        raise ValueError("a duty's intervals must be 0 < minimum <= maximum")
    return Duty(instruction, DutyKind(duty.kind), duty.min_interval_s, duty.max_interval_s)
