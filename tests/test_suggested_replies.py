"""Suggested replies: the agent ends an answer with replies the user can click; a click
sends that reply as the user's message."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kayden.api import AgentState, ToolResult, ToolStatus
from kayden.frontends.common import replies_of
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools
from tests.test_desktop_window import qapp  # noqa: F401 - a fixture

REPLIES = {"replies": ["Yes, push", "Not now"]}


async def harness(make: Any, steps: list[Step]) -> Harness:
    tools = FakeTools()
    tools.instant("read", "contents")
    h: Harness = await make(core=ScriptedCore(steps), tools=tools)
    await h.start()
    return h


def results(h: Harness) -> dict[str, ToolResult]:
    return {b.call_id: b for m in h.store.conversations[h.conversation.id].messages
            for b in m.blocks if isinstance(b, ToolResult)}


async def test_suggesting_replies_ends_the_turn_without_another_request(make):
    h = await harness(make, [
        Step(text="Committed. Push it?", calls=[call("suggest_replies", REPLIES, id="s")]),
        Step(text="never asked for")])
    await h.turn("commit it")
    assert len(h.core.requests) == 1
    assert results(h)["s"].status == ToolStatus.OK
    assert h.runtime.snapshot().state == AgentState.IDLE
    messages = h.store.conversations[h.conversation.id].messages
    [call_] = [b for b in messages[-2].blocks if getattr(b, "name", "") == "suggest_replies"]
    assert replies_of(call_) == ("Yes, push", "Not now")


async def test_suggesting_replies_with_other_calls_is_an_error(make):
    h = await harness(make, [
        Step(calls=[call("read", id="r"), call("suggest_replies", REPLIES, id="s")]),
        Step(text="Read it.")])
    await h.turn("go")
    await eventually(lambda: len(h.core.requests) == 2)
    assert results(h)["r"].status == ToolStatus.OK
    assert results(h)["s"].status == ToolStatus.ERROR and "alone" in results(h)["s"].content


@pytest.mark.parametrize("replies", [[], ["a", "b", "c", "d"], [""]])
async def test_invalid_replies_are_an_error(make, replies: list[str]):
    h = await harness(make, [
        Step(calls=[call("suggest_replies", {"replies": replies}, id="s")]),
        Step(text="ok")])
    await h.turn("go")
    await eventually(lambda: len(h.core.requests) == 2)
    assert results(h)["s"].status == ToolStatus.ERROR


@pytest.mark.usefixtures("qapp")
async def test_the_desktop_shows_buttons_that_send_the_reply(make):
    from PySide6.QtWidgets import QPushButton

    from kayden.frontends.desktop.viewmodel import Controller, ConversationModel
    from kayden.frontends.desktop.window import MainWindow, ReplyButtons

    h = await harness(make, [
        Step(text="Push it?", calls=[call("suggest_replies", REPLIES, id="s")]),
        Step(text="Pushed.")])
    controller = Controller(h.runtime, ConversationModel())
    window = MainWindow(h.runtime, controller)
    asyncio.create_task(controller.run())
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    window.show()
    await h.turn("commit it")
    [row] = await eventually(lambda: window.findChildren(ReplyButtons))
    buttons = row.findChildren(QPushButton)
    assert [b.text() for b in buttons] == ["Yes, push", "Not now"]
    assert buttons[0].property("primary") and not buttons[1].property("primary")
    assert not [i for i in controller.model.items
                if getattr(i, "name", "") == "suggest_replies"]  # not shown as a step
    buttons[0].click()
    await eventually(lambda: len(h.core.requests) == 2)
    assert "Yes, push" in repr(h.core.requests[-1].messages[-1])
    await eventually(lambda: all(r.isHidden() for r in window.findChildren(ReplyButtons)))


@pytest.mark.usefixtures("qapp")
async def test_the_buttons_come_back_when_the_conversation_is_reopened(make):
    from kayden.frontends.desktop.viewmodel import Controller, ConversationModel
    from kayden.frontends.desktop.window import MainWindow, ReplyButtons

    h = await harness(make, [
        Step(text="Push it?", calls=[call("suggest_replies", REPLIES, id="s")])])
    await h.turn("commit it")
    controller = Controller(h.runtime, ConversationModel())
    window = MainWindow(h.runtime, controller)
    asyncio.create_task(controller.run())
    window.show()
    await eventually(lambda: window.findChildren(ReplyButtons))
    assert controller.model.replies == ("Yes, push", "Not now")


async def test_the_terminal_app_shows_buttons_that_send_the_reply(make):
    from textual.widgets import Button

    from kayden.frontends.tui import KaydenApp, ReplyButtons
    from tests.test_tui import type_text, until

    h = await harness(make, [
        Step(text="Push it?", calls=[call("suggest_replies", REPLIES, id="s")]),
        Step(text="Fine."), Step(text="ok")])
    app = KaydenApp(h.runtime)
    async with app.run_test(size=(100, 40)) as pilot:
        await until(pilot, lambda: app.conversation is not None)
        await type_text(pilot, "commit it")
        await pilot.press("enter")
        await until(pilot, lambda: app.query(ReplyButtons), timeout=10)
        labels = [str(b.label) for b in app.query_one(ReplyButtons).query(Button)]
        assert labels == ["Yes, push", "Not now"]
        app.query_one(ReplyButtons).query(Button)[1].press()
        await until(pilot, lambda: len(h.core.requests) == 2, timeout=10)
        assert "Not now" in repr(h.core.requests[-1].messages[-1])
        await until(pilot, lambda: not app.query(ReplyButtons), timeout=10)


def test_replies_come_from_suggest_replies_and_ask_for_help():
    from kayden.api import ToolCall

    assert replies_of(ToolCall("a", "suggest_replies", REPLIES)) == ("Yes, push", "Not now")
    assert replies_of(ToolCall("b", "ask_for_help", {"question": "Which?", **REPLIES})) == (
        "Yes, push", "Not now")
    assert replies_of(ToolCall("c", "ask_for_help", {"question": "Which?"})) == ()
    assert replies_of(ToolCall("d", "read", REPLIES)) == ()
