"""The desktop app's widgets, offscreen, against the runtime with scripted fakes."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from kayden.api import Budget, PermissionPolicy, SettingsUpdate
from kayden.frontends.desktop.viewmodel import Controller, ConversationModel
from kayden.frontends.desktop.window import (
    AssistantBubble,
    MainWindow,
    PermissionCard,
    SettingsDialog,
    ToolCard,
    UserBubble,
    WelcomeDialog,
)
from kayden.runtime import ModuleSpec, Provider
from tests.conftest import Harness, eventually
from tests.fakes.core import Step, call

FAKE_CORE = "tests.fakes.core:factory"
PROVIDERS = (
    Provider("first", "First AI", ModuleSpec(FAKE_CORE), key_name="FIRST_API_KEY"),
    Provider("broken", "Broken AI", ModuleSpec(FAKE_CORE, {"list_error": "invalid key"}),
             key_name="BROKEN_API_KEY"),
)
TOOLS = ModuleSpec("tests.fakes.tools:factory", {"root": "/start"})


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


async def open_window(h: Harness) -> tuple[MainWindow, Controller, asyncio.Task[None]]:
    controller = Controller(h.runtime, ConversationModel())
    window = MainWindow(h.runtime, controller)
    task = asyncio.create_task(controller.run())
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    return window, controller, task


def widgets[W](window: MainWindow, kind: type[W]) -> list[W]:
    return [w for w in window._widgets.values() if isinstance(w, kind)]


def buttons(widget: Any, text: str) -> list[QPushButton]:
    return [b for b in widget.findChildren(QPushButton) if b.text() == text]


async def test_a_turn_is_rendered(make, qapp):
    h: Harness = await make()
    h.tools.instant("look", "found")
    h.core.add(Step(calls=[call("look", {"q": 1})]), Step(text="# Title\n\n| a | b |\n|---|---|"))
    await h.start()
    window, controller, _ = await open_window(h)
    window.input.setPlainText("hello")
    QTest.keyClick(window.input, Qt.Key.Key_Return)
    assert window.input.toPlainText() == ""
    await eventually(lambda: widgets(window, AssistantBubble) and not controller.model.busy)
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    (user,) = widgets(window, UserBubble)
    assert user.text.text() == "hello"
    (tool,) = widgets(window, ToolCard)
    assert tool.state.text().startswith("done") and not tool.cancel.isVisibleTo(tool)
    (reply,) = widgets(window, AssistantBubble)
    assert "Title" in reply.body.toPlainText()
    assert window.status_label.text() == "Ready" and window.send_button.text() == "Send"


async def test_permission_card_buttons_answer_the_prompt(make, qapp):
    h: Harness = await make()
    h.tools.instant("write", read_only=False)
    h.core.add(Step(calls=[call("write")]), Step(text="ok"))
    await h.start()
    window, controller, _ = await open_window(h)
    controller.send("write")
    await eventually(lambda: window.prompts.count() == 1)
    card = window.prompts.itemAt(0).widget()
    assert isinstance(card, PermissionCard)
    assert window.send_button.text() == "Stop"
    buttons(card, "Allow once")[0].click()
    await eventually(lambda: window.prompts.count() == 0 and not controller.model.busy)
    assert widgets(window, ToolCard)[0].state.text().startswith("done")


async def test_switching_is_disabled_while_busy(make, qapp):
    h: Harness = await make()
    h.tools.hanging("wait")
    h.core.add(Step(calls=[call("wait")]))
    await h.start()
    window, controller, _ = await open_window(h)
    controller.send("wait")
    await eventually(lambda: widgets(window, ToolCard))
    await eventually(lambda: not window.new_button.isEnabled())
    assert window.switch_reason.text() == "Finish or stop the current task first."
    window.send_button.click()  # Stop
    await eventually(lambda: window.new_button.isEnabled())


async def setup(make: Any) -> Harness:
    return await make(providers=PROVIDERS, provider="first", tools_module=TOOLS,
                      permissions=PermissionPolicy.DENY)


def message_box(dialog: Any) -> QMessageBox:
    (box,) = [b for b in dialog.findChildren(QMessageBox) if b.isVisible()]
    return box


async def test_loosening_in_settings_asks_first_and_no_sends_nothing(make, qapp):
    h = await setup(make)
    await h.runtime.update_settings(SettingsUpdate(budget=Budget(max_cost_usd=10.0)))
    await h.start()
    window, controller, _ = await open_window(h)
    settings = await h.runtime.settings()
    dialog = SettingsDialog(window, h.runtime, controller, settings, [], 0.0)
    dialog.open()
    dialog.limit.setText("50")
    dialog.permissions.setCurrentIndex(dialog.permissions.findData(PermissionPolicy.ASK))
    dialog._save()
    box = message_box(dialog)
    assert "$10 to $50" in box.text() and "ask me before every action" in box.text()
    box.button(QMessageBox.StandardButton.No).click()
    await asyncio.sleep(0.05)
    assert (await h.runtime.settings()).budget.max_cost_usd == 10.0
    dialog._save()
    message_box(dialog).button(QMessageBox.StandardButton.Yes).click()
    await eventually(lambda: not dialog.isVisible())
    after = await h.runtime.settings()
    assert after.budget.max_cost_usd == 50.0
    assert after.permissions is PermissionPolicy.ASK


async def test_tightening_saves_without_asking(make, qapp):
    h = await setup(make)
    await h.start()
    window, controller, _ = await open_window(h)
    dialog = SettingsDialog(window, h.runtime, controller, await h.runtime.settings(), [], 0.0)
    dialog.open()
    dialog.limit.setText("5")
    dialog._save()
    await eventually(lambda: not dialog.isVisible())
    assert (await h.runtime.settings()).budget.max_cost_usd == 5.0


async def test_welcome_saves_the_key_folder_and_limit(make, qapp, keychain, tmp_path):
    h = await setup(make)
    window, controller, _ = await open_window(h)
    settings = await h.runtime.settings()
    dialog = WelcomeDialog(window, h.runtime, controller, settings)
    accepted: list[bool] = []
    dialog.accepted.connect(lambda: accepted.append(True))
    dialog.open()
    dialog.provider.setCurrentRow(1)  # Broken AI: its key is rejected
    dialog.pages.setCurrentIndex(1)
    dialog.key.setText("bad")
    dialog.key_continue.click()
    await eventually(lambda: dialog.key_error.isVisibleTo(dialog))
    assert "BROKEN_API_KEY" in dialog.key_error.text() and dialog.pages.currentIndex() == 1
    assert (await h.runtime.settings()).provider == "first"
    dialog.pages.setCurrentIndex(0)
    dialog.provider.setCurrentRow(0)
    dialog.pages.setCurrentIndex(1)
    dialog.key.setText("sk-good")
    dialog.key_continue.click()
    await eventually(lambda: dialog.pages.currentIndex() == 2)
    assert keychain.store[("kayden", "FIRST_API_KEY")] == "sk-good"
    dialog.folder.setText(str(tmp_path / "work"))
    dialog.limit.setText("3")
    dialog.finish_button.click()
    await eventually(lambda: accepted)
    after = await h.runtime.settings()
    assert after.work_folder == str(tmp_path / "work") and (tmp_path / "work").is_dir()
    assert after.budget.max_cost_usd == 3.0
