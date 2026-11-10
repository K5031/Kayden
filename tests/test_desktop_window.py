"""The desktop app's widgets, offscreen, against the runtime with scripted fakes."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio
from typing import Any

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction, QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QMenu, QMessageBox, QPushButton

from kayden.api import Budget, PermissionPolicy, SettingsUpdate, ShuttingDown
from kayden.frontends.desktop.viewmodel import Controller, ConversationModel
from kayden.frontends.desktop.window import (
    DATE_ROLE,
    PREVIEW_ROLE,
    AssistantBubble,
    MainWindow,
    PermissionCard,
    SettingsDialog,
    ToolCard,
    UserBubble,
    WelcomeDialog,
)
from kayden.runtime import ModuleState
from kayden.runtime.config import RuntimeConfig
from kayden.runtime.setup import SetupRuntime
from tests.conftest import Harness, eventually
from tests.fakes.core import Step, call

RUNNING = {"core": "first", "memory": "fake-memory", "tools": "fake-tools",
           "frontend": "scripted-window"}


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
    return await make(modules=ModuleState(running=RUNNING, work_folder="/start"),
                      permissions=PermissionPolicy.DENY, kind="window")


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
    assert box.text() == "Save these changes?"
    assert "$10 to $50" in box.informativeText()
    assert "ask me before every action" in box.informativeText()
    box.defaultButton().click()  # Cancel
    await asyncio.sleep(0.05)
    assert (await h.runtime.settings()).budget.max_cost_usd == 10.0
    dialog._save()
    next(b for b in message_box(dialog).buttons() if b.text() == "Save").click()
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


def setup_runtime(tmp_path: Any, error: str = "tests.fakes.core:first_factory: "
                                               "FIRST_API_KEY is not set") -> SetupRuntime:
    """Nothing could be built; by default because the provider has no key (first run)."""
    state = ModuleState(running=RUNNING, work_folder="/start")
    return SetupRuntime(RuntimeConfig(data_dir=tmp_path / "data", modules=state), "window",
                        error)


async def test_welcome_saves_the_key_folder_and_limit_then_restarts(installed, qapp, keychain,
                                                                    tmp_path, monkeypatch):
    monkeypatch.delenv("FIRST_API_KEY", raising=False)
    runtime = setup_runtime(tmp_path)
    controller = Controller(runtime, ConversationModel())
    window = MainWindow(runtime, controller)
    asyncio.create_task(controller.run())
    await window.begin()
    dialog = window._dialog
    assert isinstance(dialog, WelcomeDialog)
    names = [dialog.provider.item(i).text() for i in range(dialog.provider.count())]
    assert "First AI" in names and "keyless" not in names  # only providers that take keys
    dialog.provider.setCurrentRow(names.index("Second AI"))
    dialog.pages.setCurrentIndex(1)
    dialog.key.setText("sk-good")
    dialog.key_continue.click()
    await eventually(lambda: dialog.pages.currentIndex() == 2)
    assert keychain.store[("kayden", "SECOND_API_KEY")] == "sk-good"
    dialog.folder.setText(str(tmp_path / "work"))
    dialog.limit.setText("3")
    dialog.finish_button.click()
    await eventually(lambda: runtime.restart_request is not None)
    after = await runtime.settings()
    assert after.modules["core"].chosen == "second"
    assert after.work_folder == str(tmp_path / "work") and (tmp_path / "work").is_dir()
    assert after.budget.max_cost_usd == 3.0


async def test_a_startup_error_opens_the_settings(installed, qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("FIRST_API_KEY", "k")  # not a first run
    runtime = setup_runtime(tmp_path, "the memory module 'gone' is not installed")
    controller = Controller(runtime, ConversationModel())
    window = MainWindow(runtime, controller)
    asyncio.create_task(controller.run())
    await window.begin()
    await eventually(lambda: isinstance(window._dialog, SettingsDialog))
    dialog = window._dialog
    assert "'gone' is not installed" in dialog.startup_error.text()
    assert dialog.restart_button.isVisibleTo(dialog)


async def test_choosing_the_tui_says_when_it_is_used(make, installed, qapp):
    h = await setup(make)
    await h.start()
    window, controller, _ = await open_window(h)
    dialog = SettingsDialog(window, h.runtime, controller, await h.runtime.settings(), [], 0.0)
    dialog.open()
    frontend = dialog.modules["frontend"]
    frontend.setCurrentIndex(frontend.findData("tui"))
    dialog._save()
    box = await eventually(lambda: next((b for b in window.findChildren(QMessageBox)
                                         if b.isVisible()), None))
    assert box.informativeText().startswith("Terminal (TUI) will be used the next time you run "
                                            "kayden in a terminal.")
    next(b for b in box.buttons() if b.text() == "Restart now").click()  # restarts the desktop app itself
    await h.rec.wait_for(lambda r: r.of(ShuttingDown))
    assert h.runtime.restart_request.frontend == "scripted-window"


async def test_model_menu_is_filled_before_it_opens(make, qapp):
    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)
    await eventually(lambda: [a.data() for a in window.model_menu.actions()]
                     == [m.id for m in h.core.models])
    window.model_menu.aboutToShow.emit()
    actions = window.model_menu.actions()  # there at once: an empty menu does not open
    assert [a.data() for a in actions if a.isChecked()] == [h.runtime.snapshot().model]
    other = next(a for a in actions if not a.isChecked())
    chosen = other.data()
    other.trigger()
    await eventually(lambda: h.runtime.snapshot().model == chosen)
    window.model_menu.aboutToShow.emit()
    assert [a.data() for a in window.model_menu.actions() if a.isChecked()] == [chosen]


async def test_model_menu_says_why_it_has_no_models(make, qapp):
    h: Harness = await make()
    window, _controller, _ = await open_window(h)

    async def fail() -> Any:
        raise RuntimeError("no key")

    h.runtime.list_models = fail
    await window.refresh_models()
    (only,) = window.model_menu.actions()
    assert only.text() == "no key" and not only.isEnabled()


# ── conversations and long-term memory ───────────────────────────────────


def visible[W](parent: Any, kind: type[W]) -> W:
    (found,) = [w for w in parent.findChildren(kind) if w.isVisible()]
    return found


def entries(window: MainWindow) -> list[str]:
    return [window.conversation_list.item(i).text()
            for i in range(window.conversation_list.count())]


def action(menu: QMenu, text: str) -> QAction:
    (found,) = [a for a in menu.actions() if a.text() == text and a.isVisible()]
    return found


async def test_sidebar_labels_rename_and_archive(make, qapp):
    h: Harness = await make()
    other = await h.start()
    await h.turn("What should I pack for Oslo?")
    await h.start()
    window, _controller, _ = await open_window(h)
    await window.fill_sidebar()
    assert "What should I pack for Oslo?" in entries(window)
    window.conversation_list.setCurrentRow(1)
    QApplication.sendEvent(window.conversation_list.viewport(), QContextMenuEvent(
        QContextMenuEvent.Reason.Keyboard, QPoint(0, 0), QPoint(0, 0)))
    menu = visible(window, QMenu)
    assert [a.text() for a in menu.actions() if a.text()] == [
        "Rename…", "Continue in a new conversation", "Archive", "Delete…"]
    menu.close()
    info = window.conversation_info(other.id)
    action(window.conversation_menu(info), "Rename…").trigger()
    dialog = visible(window, QInputDialog)
    dialog.setTextValue("Oslo trip")
    dialog.accept()
    await eventually(lambda: "Oslo trip" in entries(window))
    action(window.conversation_menu(window.conversation_info(other.id)), "Archive").trigger()
    await eventually(lambda: "Oslo trip" not in entries(window))
    window.show_archived.setChecked(True)
    await eventually(lambda: "Oslo trip  (archived)" in entries(window))
    menu = window.conversation_menu(window.conversation_info(other.id))
    action(menu, "Unarchive").trigger()
    await eventually(lambda: "Oslo trip" in entries(window))


async def test_sidebar_rows_show_a_preview_and_a_date(make, qapp):
    h: Harness = await make()
    named = await h.start()
    await h.turn("What should I pack for Oslo?")
    await h.runtime.rename_conversation(named.id, "Oslo trip")
    untitled = await h.start()
    await h.turn("Tidy my invoices folder")
    await h.start()
    window, _controller, _ = await open_window(h)
    await window.fill_sidebar()
    rows = {window.conversation_list.item(i).text(): window.conversation_list.item(i)
            for i in range(window.conversation_list.count())}
    assert set(rows) == {"New chat", "Tidy my invoices folder", "Oslo trip"}
    assert rows["Oslo trip"].data(PREVIEW_ROLE) == "What should I pack for Oslo?"
    assert rows["Tidy my invoices folder"].data(PREVIEW_ROLE) == ""  # the label already is
    assert ":" in rows["Oslo trip"].data(DATE_ROLE)  # updated today: the time
    assert untitled.id in window._conversations


async def test_an_empty_conversation_shows_the_empty_state(make, qapp):
    h: Harness = await make()
    h.core.add(Step(text="hi"))
    await h.start()
    window, controller, _ = await open_window(h)
    window._rebuild()
    assert window.empty_state.isVisibleTo(window)
    controller.send("hello")
    await eventually(lambda: widgets(window, AssistantBubble) and not controller.model.busy)
    assert not window.empty_state.isVisibleTo(window)


async def test_delete_asks_with_cancel_as_the_default(make, qapp):
    h: Harness = await make()
    other = await h.start()
    await h.runtime.rename_conversation(other.id, "Old notes")
    current = await h.start()
    window, _controller, _ = await open_window(h)
    await window.fill_sidebar()
    action(window.conversation_menu(window.conversation_info(current.id)), "Delete…").trigger()
    box = visible(window, QMessageBox)
    assert box.informativeText().endswith("A new chat will open.")
    box.defaultButton().click()
    await eventually(lambda: not box.isVisible())
    action(window.conversation_menu(window.conversation_info(other.id)), "Delete…").trigger()
    box = visible(window, QMessageBox)
    assert box.text() == "Delete “Old notes”?"
    assert box.informativeText() == (
        "This permanently deletes the conversation and what Kayden learned from it.")
    assert box.icon() == QMessageBox.Icon.NoIcon
    assert [b.text() for b in box.buttons()] == ["Cancel", "Delete"]
    assert box.defaultButton().text() == "Cancel"
    next(b for b in box.buttons() if b.text() == "Delete").click()
    await eventually(lambda: not any("Old notes" in e for e in entries(window)))
    assert [c.id for c in await h.runtime.list_conversations(include_archived=True)] == [
        current.id]


async def test_continue_in_a_new_conversation(make, qapp):
    h: Harness = await make()
    first = await h.start()
    window, controller, _ = await open_window(h)
    await window.fill_sidebar()
    action(window.conversation_menu(window.conversation_info(first.id)),
           "Continue in a new conversation").trigger()
    await eventually(lambda: controller.model.conversation.seed_from == first.id)


async def test_spending_is_shown_above_the_conversation(make, qapp):
    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)
    assert window.spending_label.text().endswith("spent")
    assert window.spending_label.isVisibleTo(window)


async def test_memory_dialog_add_edit_forget(make, qapp):
    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)
    await window.check_memory()
    assert window.memory_action.isVisible()
    await h.runtime.add_memory("Likes tea")
    window.open_memory()
    dialog = await eventually(lambda: window.memory_dialog)
    await eventually(lambda: dialog.entries.count() == 1)
    assert dialog.entries.item(0).text() == "Likes tea"
    dialog.add_button.click()
    editor = visible(dialog, QInputDialog)
    editor.setTextValue("Lives in Oslo")
    editor.accept()
    await eventually(lambda: dialog.entries.count() == 2)
    dialog.entries.setCurrentRow(0)
    dialog.edit_button.click()
    editor = visible(dialog, QInputDialog)
    editor.setTextValue("Likes green tea")
    editor.accept()
    await eventually(lambda: dialog.entries.item(0).text() == "Likes green tea")
    dialog.entries.setCurrentRow(0)
    dialog.forget_button.click()
    box = visible(dialog, QMessageBox)
    assert "stay until you delete their conversation" in box.informativeText()
    next(b for b in box.buttons() if b.text() == "Forget").click()
    await eventually(lambda: dialog.entries.count() == 1)
    assert [e.text for e in await h.runtime.memories()] == ["Lives in Oslo"]


async def test_unsupported_memory_hides_memory_and_delete(make, qapp):
    h: Harness = await make(memory_kw={"editable": False})
    other = await h.start()
    await h.start()
    window, controller, _ = await open_window(h)
    await window.check_memory()
    assert not window.memory_action.isVisible()
    await window.fill_sidebar()
    action(window.conversation_menu(window.conversation_info(other.id)), "Delete…").trigger()
    next(b for b in visible(window, QMessageBox).buttons() if b.text() == "Delete").click()
    await eventually(lambda: not controller.can_delete)
    assert not [a for a in window.conversation_menu(window.conversation_info(other.id)).actions()
                if a.text() == "Delete…" and a.isVisible()]
