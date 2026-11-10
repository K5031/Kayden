"""The desktop app's widgets, offscreen, against the runtime with scripted fakes."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio
import itertools
from typing import Any

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction, QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton

from kayden.api import Budget, PermissionPolicy, SettingsUpdate, ShuttingDown
from kayden.frontends.desktop import app_menu, theme
from kayden.frontends.desktop import window as window_module
from kayden.frontends.desktop.viewmodel import AssistantItem, Controller, ConversationModel
from kayden.frontends.desktop.window import (
    DATE_ROLE,
    PREVIEW_ROLE,
    AssistantBubble,
    MainWindow,
    PermissionCard,
    SettingsDialog,
    StepGroup,
    ToolCard,
    UserBubble,
    WelcomeDialog,
)
from kayden.runtime import ModuleState
from kayden.runtime.config import RuntimeConfig
from kayden.runtime.setup import SetupRuntime
from tests.conftest import Harness, eventually
from tests.fakes.core import Step, call
from tests.fakes.tools import FakeTools

RUNNING = {"core": "first", "memory": "fake-memory", "tools": "fake-tools",
           "frontend": "scripted-window"}


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """Styled as the launcher styles it: sizes and layout depend on the style sheet."""
    app = QApplication.instance() or QApplication([])
    theme.apply(app, dark=False)
    return app


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
    window.show()  # as the launcher does, before the welcome opens
    asyncio.create_task(controller.run())
    await window.begin()
    dialog = window._dialog
    assert isinstance(dialog, WelcomeDialog)
    names = [dialog.provider.item(i).text() for i in range(dialog.provider.count())]
    assert "First AI" in names and "keyless" not in names  # only providers that take keys
    await asyncio.sleep(0)
    rows = [dialog.provider.visualItemRect(dialog.provider.item(i)) for i in range(len(names))]
    assert all(below.top() >= above.bottom() for above, below in itertools.pairwise(rows)), (
        "provider rows overlap")
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


async def test_settings_offer_no_memory_choice(make, installed, qapp):
    h = await setup(make)
    await h.start()
    window, controller, _ = await open_window(h)
    dialog = SettingsDialog(window, h.runtime, controller, await h.runtime.settings(), [], 0.0)
    assert set(dialog.modules) == {"core", "tools", "frontend"}
    assert "memory" not in dialog.form().modules


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


async def test_kayden_can_be_added_to_the_app_menu(make, qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(app_menu, "_reload_icons", lambda: None)  # not the real session
    h: Harness = await make()
    await h.start()
    window, controller, _ = await open_window(h)
    add = window.app_menu_action
    assert add.text() == "&Add to App Menu" and add.isVisible()
    add.trigger()
    await eventually(lambda: not add.isVisible())
    assert (tmp_path / "applications/io.github.k5031.kayden.desktop").exists()
    assert "app menu" in controller.model.items[-1].text
    other, _controller, _ = await open_window(h)
    assert not other.app_menu_action.isVisible()  # already there


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
        "This permanently deletes the conversation. What Kayden remembered from it stays in Memory.")
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


async def test_the_folder_is_shown_and_choosing_another_starts_a_chat_there(make, qapp,
                                                                           tmp_path):
    default, project, other = (tmp_path / n for n in ("default", "project", "other"))
    for folder in (default, project, other):
        folder.mkdir()
    tools = FakeTools()
    tools.folders = []
    h: Harness = await make(tools=tools, modules=ModuleState(work_folder=str(default)))
    h.core.add(Step(text="hi"))
    empty = await h.start()
    window, controller, _ = await open_window(h)
    await eventually(lambda: window.folder_button.text() == "default")
    assert window.folder_button.isVisibleTo(window)
    assert str(default) in window.folder_button.toolTip()

    window.folder_chosen(str(project))  # an empty chat moves: no blank entry is left
    await eventually(lambda: controller.model.conversation.folder == str(project))
    await eventually(lambda: window.folder_button.text() == "project")
    listed = await h.runtime.list_conversations()
    assert [c.id for c in listed] == [controller.model.conversation.id] != [empty.id]

    controller.send("hello")
    await eventually(lambda: widgets(window, AssistantBubble) and not controller.model.busy)
    talked = controller.model.conversation.id
    window.folder_chosen(str(other))  # a chat with messages stays; a new one starts
    await eventually(lambda: controller.model.conversation.folder == str(other))
    assert talked in [c.id for c in await h.runtime.list_conversations()]


async def test_the_folder_button_is_hidden_for_tools_with_a_fixed_folder(make, qapp):
    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)
    window._refresh_status()
    assert not window.folder_button.isVisibleTo(window)


async def test_a_command_shows_its_description_with_the_command_behind_it(make, qapp):
    h: Harness = await make(permissions=PermissionPolicy.ASK)
    h.tools.instant("shell", read_only=False)
    h.core.add(Step(calls=[call("shell", {"command": "rm -rf build",
                                          "description": "Remove the old build"})]),
               Step(text="done"))
    await h.start()
    window, controller, _ = await open_window(h)
    controller.send("clean")
    await eventually(lambda: window.prompts.count() == 1)
    card = window.prompts.itemAt(0).widget()
    texts = [label.text() for label in card.findChildren(QLabel)]
    assert "Kayden wants to: Remove the old build" in texts
    assert "rm -rf build" in texts  # what actually runs, on its own line
    [tool] = widgets(window, ToolCard)
    assert tool.title.text() == "Remove the old build"
    assert tool.title.toolTip() == "shell: rm -rf build"
    assert tool.name.isHidden()  # the sentence says what the tool is


def test_a_reply_with_nothing_to_show_is_not_drawn(qapp):
    """Claude's thinking can arrive with no readable text, in a turn that only calls a tool."""
    empty = AssistantBubble(AssistantItem(reasoning="", final=True))
    assert empty.isHidden()
    for item in (AssistantItem(text="Hi", final=True),
                 AssistantItem(reasoning="Let me think", final=True),
                 AssistantItem(interrupted=True, final=True)):
        assert not AssistantBubble(item).isHidden()
    filling = AssistantItem()
    bubble = AssistantBubble(filling)
    filling.text, filling.final = "Hello", True
    bubble.refresh()
    assert not bubble.isHidden()


async def test_a_finished_step_is_in_the_past_tense_without_a_permission_line(make, qapp):
    h: Harness = await make()
    h.tools.instant("look")
    h.core.add(Step(calls=[call("look", {"q": "x", "description": "Search the web for x"})]),
               Step(text="done"))
    await h.start()
    window, controller, _ = await open_window(h)
    controller.send("go")
    await eventually(lambda: not controller.model.busy and widgets(window, ToolCard))
    [card] = widgets(window, ToolCard)
    await eventually(lambda: card.title.text() == "Searched the web for x")
    assert not hasattr(card, "notes")


def test_replies_have_no_avatar_and_roomy_lines(qapp):
    bubble = AssistantBubble(AssistantItem(text="One.\n\n- a\n- b\n\nTwo.", final=True))
    assert not [w for w in bubble.findChildren(QLabel) if w.objectName() == "avatar"]
    block = bubble.body.document().begin()
    assert block.blockFormat().lineHeight() >= 140  # percent of the font's line
    assert block.blockFormat().bottomMargin() >= 8


@pytest.fixture
def unzoomed(qapp):
    yield
    theme.set_zoom(qapp, 1.0)


def test_zoom_scales_the_text(qapp, unzoomed):
    base = qapp.font().pointSizeF()
    theme.set_zoom(qapp, 1.5)
    assert qapp.font().pointSizeF() == pytest.approx(base * 1.5)
    assert f"{base * 1.5 * 1.45:.1f}pt" in qapp.styleSheet()
    theme.apply(qapp, dark=True)  # restyling keeps the zoom
    assert qapp.font().pointSizeF() == pytest.approx(base * 1.5)
    theme.apply(qapp, dark=False)
    theme.set_zoom(qapp, 1.0)
    assert qapp.font().pointSizeF() == pytest.approx(base)


def test_zoom_steps_stop_at_the_ends():
    assert theme.zoom_step(1.0, 1) > 1.0 and theme.zoom_step(1.0, -1) < 1.0
    assert theme.zoom_step(theme.ZOOM_LEVELS[-1], 1) == theme.ZOOM_LEVELS[-1]
    assert theme.zoom_step(theme.ZOOM_LEVELS[0], -1) == theme.ZOOM_LEVELS[0]
    assert theme.zoom_step(1.3, 1) == 1.5  # a level saved by hand moves to the next one


async def test_ctrl_plus_minus_and_zero_zoom_and_are_remembered(make, qapp, unzoomed, tmp_path):
    h: Harness = await make()
    await h.start()
    controller = Controller(h.runtime, ConversationModel())
    saved = tmp_path / "desktop.json"
    window = MainWindow(h.runtime, controller, zoom_file=saved)
    actions = {a.text().replace("&", ""): a for a in window.findChildren(QAction)}
    actions["Zoom In"].trigger()
    actions["Zoom In"].trigger()
    assert theme.zoom() == theme.zoom_step(theme.zoom_step(1.0, 1), 1)
    await eventually(lambda: saved.exists() and str(theme.zoom()) in saved.read_text())
    actions["Zoom Out"].trigger()
    assert theme.zoom() == theme.zoom_step(1.0, 1)
    actions["Actual Size"].trigger()
    assert theme.zoom() == 1.0
    actions["Zoom In"].trigger()
    await window.save_pending_zoom()  # as when the window closes right away
    assert '"zoom": 1.1' in saved.read_text()
    actions["Actual Size"].trigger()
    assert "Ctrl+=" in [k.toString() for k in actions["Zoom In"].shortcuts()]
    assert actions["Actual Size"].shortcut().toString() == "Ctrl+0"
    await eventually(lambda: '"zoom": 1.0' in saved.read_text())


async def test_a_saved_zoom_is_read_back(tmp_path):
    saved = tmp_path / "desktop.json"
    assert await window_module.load_zoom(saved) == 1.0
    saved.write_text('{"zoom": 1.25}')
    assert await window_module.load_zoom(saved) == 1.25
    saved.write_text("not json")
    assert await window_module.load_zoom(saved) == 1.0


def groups(window: MainWindow) -> list[StepGroup]:
    layout = window.items_layout
    return [w for i in range(layout.count())
            if isinstance(w := layout.itemAt(i).widget(), StepGroup)]


async def test_steps_fold_into_one_line_until_opened(make, qapp):
    h: Harness = await make()
    h.tools.instant("look", "found")
    h.tools.failing("open")
    h.core.add(Step(calls=[call("look", {"q": 1, "description": "Search the web"}),
                           call("open", {"url": "u", "description": "Open the page"})]),
               Step(text="Here it is."))
    await h.start()
    window, controller, _ = await open_window(h)
    window.show()
    controller.send("go")
    await eventually(lambda: widgets(window, AssistantBubble) and not controller.model.busy)
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    (group,) = groups(window)
    assert group.title.text() == "2 steps" and group.state.text() == "1 failed"
    cards = widgets(window, ToolCard)
    assert len(cards) == 2 and not any(card.isVisible() for card in cards)
    group.toggle.click()
    assert all(card.isVisible() for card in cards)
    assert not cards[0].state.isVisible() and cards[1].state.text().startswith("failed")
    reply = widgets(window, AssistantBubble)[0]
    layout = window.items_layout
    assert layout.indexOf(reply) == layout.indexOf(group) + 1  # the reply stays outside
    window.close()


async def test_a_running_group_shows_its_current_step(make, qapp):
    h: Harness = await make()
    h.tools.instant("look", "found")
    h.tools.hanging("wait")
    h.core.add(Step(calls=[call("look", {"description": "Search the web"})]),
               Step(calls=[call("wait", {"description": "Wait for the build"})]))
    await h.start()
    window, controller, _ = await open_window(h)
    controller.send("go")
    await eventually(lambda: groups(window) and groups(window)[0].title.text()
                     == "Wait for the build")
    (group,) = groups(window)
    assert group.state.text() == "running" and group.cancel.isVisibleTo(group)
    group.cancel.click()
    await eventually(lambda: not controller.model.busy)
    await eventually(lambda: group.title.text() == "2 steps")
    assert group.state.text() == "1 cancelled" and not group.cancel.isVisibleTo(group)


async def test_a_replaced_streaming_reply_stays_hidden(make, qapp):
    h: Harness = await make()
    h.core.add(Step(text="Reply text."))
    await h.start()
    window, controller, _ = await open_window(h)
    window.show()
    controller.send("q")
    await eventually(lambda: widgets(window, AssistantBubble) and not controller.model.busy)
    await asyncio.sleep(0.2)  # past the redraw delay of the streamed bubble
    qapp.processEvents()
    shown = [b for b in window.findChildren(AssistantBubble) if b.isVisible()]
    assert shown == widgets(window, AssistantBubble)
    window.close()


async def test_a_burst_of_changes_is_laid_out_once(make, qapp, monkeypatch):
    h: Harness = await make()
    await h.start()
    window, controller, _ = await open_window(h)
    await asyncio.sleep(0)
    arranged = []
    original = window._arrange
    monkeypatch.setattr(window, "_arrange", lambda: (arranged.append(1), original()))
    for n in range(20):  # as a page of earlier messages arrives
        controller.model.notify(f"note {n}", "info")
    await asyncio.sleep(0)
    assert len(arranged) == 1 and window.items_layout.count() >= 20
