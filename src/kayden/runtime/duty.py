"""Duties: the prompt section, the plan the agent writes with update_plan, and when the
duty wakes next."""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from kayden.api import NOTICE_PREFIX, Duty, DutyKind, PlanItem, PlanStatus

TURNED_ON = "the duty was turned on"
DUE = "a plan item is due"
CHECK_IN = "scheduled check-in"
FINISHED = "background work finished"
OPEN = (PlanStatus.TODO, PlanStatus.DOING)
CLOSE_PLAN = (
    "Steps are still open. Before you end your turn, bring the plan up to date with "
    "update_plan: mark what you finished done and what you gave up dropped, and finish "
    "what you can. Leave a step open only when it waits for the user or for background "
    "work.")


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


def prompt(duty: Duty | None, plan: Sequence[PlanItem]) -> str:
    """The system prompt section for a conversation with a duty or a plan."""
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
    if plan:
        lines = [f"- [{i.status}] {i.text}" + (f" (due {local(i.due)})" if i.due else "")
                 + (f" — {i.note}" if i.note else "") for i in plan]
        section = "## Plan\n" + "\n".join(lines)
        if duty is None and any(i.status in OPEN for i in plan):
            section += f"\n\n{CLOSE_PLAN}"
        parts.append(section)
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
