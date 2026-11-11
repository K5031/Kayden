"""The wrap-up card: the agent ends its turn with a recap and, when the user has a decision
to make, a question and options to click. An option sends its label as the user's message,
or runs the one call it carries."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kayden.api import (
    AgentState,
    OperationResult,
    PermissionPolicy,
    ProposalMade,
    ToolCall,
    ToolResult,
    ToolStatus,
)
from kayden.frontends.common import Card, card_of
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools
from tests.test_desktop_window import qapp  # noqa: F401 - a fixture

RECAP = "Committed the fix. Not pushed yet."
PUSH = {"name": "write", "arguments": {"path": "remote"}}
WRAP = {"recap": RECAP, "question": "Push it?",
        "options": [{"label": "Yes, push"}, {"label": "Not now"}]}
ACTION = {**WRAP, "options": [{"label": "Yes, push", "call": PUSH}, {"label": "Not now"}]}


async def harness(make: Any, steps: list[Step]) -> Harness:
    tools = FakeTools()
    tools.instant("read", "contents")
    tools.instant("write", "pushed", read_only=False)
    h: Harness = await make(core=ScriptedCore(steps), tools=tools,
                            permissions=PermissionPolicy.ALLOW_READ_ONLY)
    await h.start()
    return h


def blocks(h: Harness) -> list[Any]:
    return [b for m in h.store.conversations[h.conversation.id].messages for b in m.blocks]


def results(h: Harness) -> dict[str, ToolResult]:
    return {b.call_id: b for b in blocks(h) if isinstance(b, ToolResult)}


def outcomes(h: Harness) -> dict[str, OperationResult]:
    return {b.call_id: b for b in blocks(h) if isinstance(b, OperationResult)}


async def test_wrapping_up_ends_the_turn_without_another_request(make):
    h = await harness(make, [
        Step(text="Committed.", calls=[call("wrap_up", WRAP, id="w")]),
        Step(text="never asked for")])
    await h.turn("commit it")
    assert len(h.core.requests) == 1
    assert results(h)["w"].status == ToolStatus.OK
    assert h.runtime.snapshot().state == AgentState.IDLE
    [wrap] = [b for b in blocks(h) if getattr(b, "name", "") == "wrap_up"]
    assert card_of(wrap) == Card("w", RECAP, "Push it?", ("Yes, push", "Not now"), None)


async def test_wrapping_up_with_other_calls_is_an_error(make):
    h = await harness(make, [
        Step(calls=[call("read", id="r"), call("wrap_up", WRAP, id="w")]),
        Step(text="Read it.")])
    await h.turn("go")
    await eventually(lambda: len(h.core.requests) == 2)
    assert results(h)["r"].status == ToolStatus.OK
    assert results(h)["w"].status == ToolStatus.ERROR and "alone" in results(h)["w"].content


@pytest.mark.parametrize("arguments", [
    {"question": "Push it?", "options": [{"label": "Yes"}]},  # no recap
    {"recap": RECAP, "options": []},
    {"recap": RECAP, "options": [{"label": str(i)} for i in range(4)]},
    {"recap": RECAP, "options": [{"label": ""}]},
    {"recap": RECAP, "options": [{"label": "A", "call": PUSH}, {"label": "B", "call": PUSH}]},
    {"recap": RECAP, "options": [{"label": "A", "call": {"name": "nope"}}]},
])
async def test_an_invalid_card_is_an_error(make, arguments: dict[str, Any]):
    h = await harness(make, [
        Step(calls=[call("wrap_up", arguments, id="w")]), Step(text="ok")])
    await h.turn("go")
    await eventually(lambda: len(h.core.requests) == 2)
    assert results(h)["w"].status == ToolStatus.ERROR
    assert h.runtime.snapshot().proposals == ()


async def test_an_option_with_a_call_runs_it_when_clicked(make):
    h = await harness(make, [
        Step(text="Committed.", calls=[call("wrap_up", ACTION, id="w")]),
        Step(text="Pushed.")])
    await h.turn("commit it")
    assert len(h.core.requests) == 1  # the turn ended
    assert results(h)["w"].status == ToolStatus.BACKGROUNDED
    [made] = h.rec.of(ProposalMade)
    assert made.proposal.card and made.proposal.id == "w" and made.proposal.call.name == "write"
    assert h.tools.started == []
    await h.runtime.resolve_proposal("w", True)
    outcome = await eventually(lambda: outcomes(h).get("w"))
    assert outcome.status == ToolStatus.OK and "pushed" in outcome.content
    await eventually(lambda: len(h.core.requests) == 2)  # woken with the result


async def test_writing_instead_withdraws_the_option_before_the_message(make):
    h = await harness(make, [
        Step(text="Committed.", calls=[call("wrap_up", ACTION, id="w")]),
        Step(text="Fine, not pushing."), Step(text="never asked for")])
    await h.turn("commit it")
    await h.runtime.send("Leave it for now")
    await eventually(lambda: h.runtime.snapshot().state == AgentState.IDLE
                     and len(h.core.requests) == 2)
    assert h.runtime.snapshot().proposals == ()
    assert h.tools.started == []
    sent = h.core.requests[1].messages
    texts = [repr(m) for m in sent]
    withdrawn = next(i for i, t in enumerate(texts) if "OperationResult" in t and "'w'" in t)
    asked = next(i for i, t in enumerate(texts) if "Leave it for now" in t)
    assert withdrawn < asked
    await asyncio.sleep(0.2)
    assert len(h.core.requests) == 2  # the withdrawal woke nothing on its own


@pytest.mark.usefixtures("qapp")
async def test_the_desktop_shows_the_card_and_sends_a_reply(make):
    from PySide6.QtWidgets import QLabel, QPushButton, QToolButton

    from kayden.frontends.desktop.viewmodel import Controller, ConversationModel
    from kayden.frontends.desktop.window import MainWindow, WrapUpCard

    h = await harness(make, [
        Step(text="Committed.", calls=[call("wrap_up", WRAP, id="w")]),
        Step(text="Pushed.")])
    controller = Controller(h.runtime, ConversationModel())
    window = MainWindow(h.runtime, controller)
    asyncio.create_task(controller.run())
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    window.show()
    await h.turn("commit it")
    [card] = await eventually(lambda: window.findChildren(WrapUpCard))
    assert [label.text() for label in card.findChildren(QLabel)] == [RECAP, "Push it?"]
    buttons = card.findChildren(QPushButton)
    assert [b.text() for b in buttons] == ["Yes, push", "Not now"]
    assert buttons[0].property("primary") and not buttons[1].property("primary")
    assert card.findChildren(QToolButton)[0].accessibleName() == "Close"
    assert not [i for i in controller.model.items
                if getattr(i, "name", "") == "wrap_up"]  # not shown as a step
    buttons[0].click()
    await eventually(lambda: len(h.core.requests) == 2)
    assert "Yes, push" in repr(h.core.requests[-1].messages[-1])
    await eventually(lambda: all(c.isHidden() for c in window.findChildren(WrapUpCard)))


@pytest.mark.usefixtures("qapp")
async def test_the_desktop_runs_the_option_with_a_call(make):
    from PySide6.QtWidgets import QPushButton

    from kayden.frontends.desktop.viewmodel import Controller, ConversationModel
    from kayden.frontends.desktop.window import MainWindow, ProposalCard, WrapUpCard

    h = await harness(make, [
        Step(text="Committed.", calls=[call("wrap_up", ACTION, id="w")]),
        Step(text="Pushed.")])
    controller = Controller(h.runtime, ConversationModel())
    window = MainWindow(h.runtime, controller)
    asyncio.create_task(controller.run())
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    window.show()
    await h.turn("commit it")
    await eventually(lambda: "w" in controller.model.proposals)
    await eventually(lambda: controller.model.card is not None)
    card = window.findChildren(WrapUpCard)[-1]
    assert not window.findChildren(ProposalCard)
    card.findChildren(QPushButton)[0].click()
    outcome = await eventually(lambda: outcomes(h).get("w"))
    assert "pushed" in outcome.content
    await eventually(lambda: len(h.core.requests) == 2)
    assert "Yes, push" not in [getattr(b, "text", None) for b in blocks(h)]  # not sent


@pytest.mark.usefixtures("qapp")
async def test_closing_the_card_puts_it_away(make):
    from PySide6.QtWidgets import QToolButton

    from kayden.frontends.desktop.viewmodel import Controller, ConversationModel
    from kayden.frontends.desktop.window import MainWindow, WrapUpCard

    h = await harness(make, [Step(text="Committed.", calls=[call("wrap_up", WRAP, id="w")])])
    await h.turn("commit it")
    controller = Controller(h.runtime, ConversationModel())
    window = MainWindow(h.runtime, controller)
    asyncio.create_task(controller.run())
    window.show()
    await eventually(lambda: controller.model.card is not None)  # shown on reopening
    card = window.findChildren(WrapUpCard)[-1]
    card.findChildren(QToolButton)[0].click()
    await eventually(lambda: all(c.isHidden() for c in window.findChildren(WrapUpCard)))
    assert controller.model.card is None and len(h.core.requests) == 1


async def test_the_terminal_app_shows_the_card(make):
    from textual.widgets import Button

    from kayden.frontends.tui import KaydenApp, WrapUpPrompt
    from tests.test_tui import type_text, until

    h = await harness(make, [
        Step(text="Committed.", calls=[call("wrap_up", WRAP, id="w")]),
        Step(text="Fine."), Step(text="ok")])
    app = KaydenApp(h.runtime)
    async with app.run_test(size=(100, 40)) as pilot:
        await until(pilot, lambda: app.conversation is not None)
        await type_text(pilot, "commit it")
        await pilot.press("enter")
        await until(pilot, lambda: app.query(WrapUpPrompt), timeout=10)
        labels = [str(b.label) for b in app.query_one(WrapUpPrompt).query(Button)]
        assert labels == ["Yes, push", "Not now", "✕"]
        app.query_one(WrapUpPrompt).query(Button)[1].press()
        await until(pilot, lambda: len(h.core.requests) == 2, timeout=10)
        assert "Not now" in repr(h.core.requests[-1].messages[-1])
        await until(pilot, lambda: not app.query(WrapUpPrompt), timeout=10)


def test_cards_come_from_wrap_up_and_ask_for_help():
    assert card_of(ToolCall("a", "wrap_up", ACTION)) == Card(
        "a", RECAP, "Push it?", ("Yes, push", "Not now"), "Yes, push")
    assert card_of(ToolCall("b", "wrap_up", {"recap": RECAP})) == Card("b", RECAP, "", (), None)
    assert card_of(ToolCall("c", "ask_for_help", {"question": "Which?", "replies": ["A"]})) == (
        Card("c", "", "Which?", ("A",), None))
    assert card_of(ToolCall("d", "ask_for_help", {"question": "Which?"})) is None
    assert card_of(ToolCall("e", "read", WRAP)) is None
