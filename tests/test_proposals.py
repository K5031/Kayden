"""Proposals: the agent proposes a tool call; the user approves or dismisses it; Kayden
runs exactly that call on approval and wakes the agent with the outcome."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kayden.api import (
    AgentState,
    OperationResult,
    PermissionPolicy,
    PermissionRequested,
    ProposalMade,
    ProposalResolved,
    RuntimeStateError,
    ToolStatus,
)
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools
from tests.test_desktop_window import qapp  # noqa: F401 - a fixture

WRITE = {"name": "write", "arguments": {"path": "notes.md", "step_summary": "Tidy notes.md"}}


async def harness(make: Any, steps: list[Step], **config: Any) -> Harness:
    tools = FakeTools()
    tools.instant("write", "written", read_only=False)
    tools.slow("fetch", 0.3)
    h: Harness = await make(core=ScriptedCore(steps), tools=tools,
                            permissions=config.pop("permissions", PermissionPolicy.ASK),
                            **config)
    await h.start()
    return h


def outcomes(h: Harness) -> dict[str, OperationResult]:
    return {b.call_id: b for m in h.store.conversations[h.conversation.id].messages
            for b in m.blocks if isinstance(b, OperationResult)}


async def test_a_proposal_returns_at_once_and_shows_a_card(make):
    h = await harness(make, [
        Step(calls=[call("propose", {"call": WRITE, "reason": "They are messy"}, id="p")]),
        Step(text="I proposed tidying your notes.")])
    await h.turn("hi")
    results = await h.results()
    assert results["p"].status == ToolStatus.BACKGROUNDED
    assert "dismissed" in results["p"].content  # tells the agent what comes back
    [made] = h.rec.of(ProposalMade)
    proposal = made.proposal
    assert (proposal.id, proposal.call.name, proposal.reason) == ("p", "write",
                                                                   "They are messy")
    assert h.runtime.snapshot().proposals == (proposal,)
    assert h.runtime.snapshot().state == AgentState.IDLE
    assert h.tools.started == []  # nothing ran


async def test_approving_runs_exactly_that_call_without_a_prompt(make):
    h = await harness(make, [
        Step(calls=[call("propose", {"call": WRITE, "reason": "Messy"}, id="p")]),
        Step(text="Proposed."), Step(text="Done tidying.")])
    await h.turn("hi")
    await h.runtime.resolve_proposal("p", True)
    result = await eventually(lambda: outcomes(h).get("p"))
    assert result.status == ToolStatus.OK and "written" in result.content
    [received] = h.tools.received
    assert received.arguments == {"path": "notes.md"}  # the step sentence removed
    assert h.rec.of(PermissionRequested) == []  # the click was the permission
    assert h.rec.of(ProposalResolved)[-1] == ProposalResolved("p", True)
    await eventually(lambda: len(h.core.requests) == 3)  # the outcome woke the agent
    assert h.runtime.snapshot().proposals == ()


async def test_dismissing_tells_the_agent(make):
    h = await harness(make, [
        Step(calls=[call("propose", {"call": WRITE, "reason": "Messy"}, id="p")]),
        Step(text="Proposed."), Step(text="Fine, leaving them.")])
    await h.turn("hi")
    await h.runtime.resolve_proposal("p", False)
    result = await eventually(lambda: outcomes(h).get("p"))
    assert "dismissed" in result.content and h.tools.started == []
    await eventually(lambda: len(h.core.requests) == 3)
    with pytest.raises(RuntimeStateError):
        await h.runtime.resolve_proposal("p", True)


async def test_invalid_proposals_are_errors(make):
    h = await harness(make, [
        Step(calls=[call("propose", {"call": {"name": "nope"}, "reason": "x"}, id="a"),
                    call("propose", {"call": {"name": "op_status", "arguments": {}},
                                     "reason": "x"}, id="b")]),
        Step(text="ok")])
    await h.turn("hi")
    results = await h.results()
    assert results["a"].status == ToolStatus.ERROR and "nope" in results["a"].content
    assert results["b"].status == ToolStatus.ERROR
    assert h.rec.of(ProposalMade) == []


async def test_a_pending_proposal_does_not_hold_back_other_wake_ups(make):
    h = await harness(make, [
        Step(calls=[call("propose", {"call": WRITE, "reason": "Messy"}, id="p"),
                    call("fetch", id="f")]),
        Step(text="Waiting."), Step(text="Fetched.")], permissions=PermissionPolicy.ALLOW_READ_ONLY)
    await h.turn("hi")
    await eventually(lambda: len(h.core.requests) == 3)  # woken by fetch alone
    assert h.runtime.snapshot().proposals  # still pending


async def test_withdrawing_and_stop_remove_the_card(make):
    h = await harness(make, [
        Step(calls=[call("propose", {"call": WRITE, "reason": "Messy"}, id="p")]),
        Step(text="Proposed."),
        Step(calls=[call("op_cancel", {"call_id": "p"})]), Step(text="Withdrawn."),
        Step(calls=[call("propose", {"call": WRITE, "reason": "Again"}, id="q")]),
        Step(text="Proposed again.")])
    await h.turn("hi")
    await h.turn("never mind")
    await eventually(lambda: ProposalResolved("p", None) in h.rec.of(ProposalResolved))
    await h.turn("one more")
    await eventually(lambda: h.runtime.snapshot().proposals)
    await h.runtime.stop()
    await eventually(lambda: ProposalResolved("q", None) in h.rec.of(ProposalResolved))
    assert h.runtime.snapshot().proposals == ()
    await asyncio.sleep(0)
    assert h.tools.started == []


@pytest.mark.usefixtures("qapp")
async def test_the_desktop_shows_a_card_that_approves(make):
    from PySide6.QtWidgets import QLabel, QPushButton

    from kayden.frontends.desktop.viewmodel import Controller, ConversationModel
    from kayden.frontends.desktop.window import MainWindow, ProposalCard

    h = await harness(make, [
        Step(calls=[call("propose", {"call": WRITE, "reason": "They are messy"}, id="p")]),
        Step(text="Proposed."), Step(text="Tidied.")])
    controller = Controller(h.runtime, ConversationModel())
    window = MainWindow(h.runtime, controller)
    asyncio.create_task(controller.run())
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    await h.turn("hi")
    window.show()
    [card] = await eventually(lambda: window.findChildren(ProposalCard))
    texts = " ".join(label.text() for label in card.findChildren(QLabel))
    assert "Kayden proposes: Tidy notes.md" in texts and "They are messy" in texts
    [approve] = [b for b in card.findChildren(QPushButton) if b.text() == "Approve"]
    approve.click()
    await eventually(lambda: outcomes(h).get("p"))
    await eventually(lambda: all(c.isHidden() for c in window.findChildren(ProposalCard)))


async def test_the_terminal_app_approves_with_a_command(make):
    from kayden.frontends.tui import KaydenApp, ProposalPrompt
    from tests.test_tui import type_text, until

    h = await harness(make, [
        Step(calls=[call("propose", {"call": WRITE, "reason": "They are messy"}, id="p")]),
        Step(text="Proposed."), Step(text="Tidied.")])
    app = KaydenApp(h.runtime)
    async with app.run_test(size=(100, 40)) as pilot:
        await until(pilot, lambda: app.conversation is not None)
        await type_text(pilot, "hi")
        await pilot.press("enter")
        await until(pilot, lambda: app.query(ProposalPrompt), timeout=10)
        await type_text(pilot, "/approve")
        await pilot.press("enter")
        await until(pilot, lambda: outcomes(h).get("p") is not None, timeout=10)
        await until(pilot, lambda: not app.query(ProposalPrompt), timeout=10)
