"""The desktop app's widgets, offscreen, against the runtime with scripted fakes."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio
import itertools
from collections.abc import Callable
from typing import Any

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction, QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton

from kayden.api import (
    AgentState,
    Budget,
    ConversationInfo,
    PermissionPolicy,
    Role,
    SettingsUpdate,
    ShuttingDown,
)
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
    app.setQuitOnLastWindowClosed(False)  # the launcher's setting: the runtime ends the loop
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
    assert window.send_button.text() == "Send"


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
    assert entries(window) == ["What should I pack for Oslo?"]  # the new chat is not listed
    window.conversation_list.setCurrentRow(0)
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
    assert set(rows) == {"Tidy my invoices folder", "Oslo trip"}
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
    current = await h.chat()
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
    first = await h.chat()
    window, controller, _ = await open_window(h)
    await window.fill_sidebar()
    action(window.conversation_menu(window.conversation_info(first.id)),
           "Continue in a new conversation").trigger()
    await eventually(lambda: controller.model.conversation.seed_from == first.id)


async def test_memory_dialog_add_edit_delete_notes(make, qapp):
    from kayden.api import PROFILE
    from kayden.frontends.desktop.window import NoteDialog

    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)
    window.show()
    await window.check_memory()
    assert window.memory_action.isVisible()
    await h.runtime.save_note(PROFILE, "", "Likes tea")
    window.open_memory()
    dialog = await eventually(lambda: window.memory_dialog)
    await eventually(lambda: dialog.entries.count() == 1)
    assert dialog.entries.item(0).text() == PROFILE
    dialog.entries.setCurrentRow(0)
    assert not dialog.forget_button.isEnabled()  # the profile stays
    dialog.add_button.click()
    editor = visible(dialog, NoteDialog)
    editor.title.setText("Trip")
    editor.description.setText("The Oslo trip")
    editor.text.setPlainText("Leaves Friday")
    editor.save_button.click()
    await eventually(lambda: dialog.entries.count() == 2)
    dialog.entries.setCurrentRow(1)
    dialog.edit_button.click()
    editor = visible(dialog, NoteDialog)
    assert editor.text.toPlainText() == "Leaves Friday"
    editor.text.setPlainText("Leaves Saturday")
    editor.save_button.click()
    await eventually(lambda: (h.store.notes()[1].text == "Leaves Saturday"))
    await eventually(lambda: dialog.entries.count() == 2)
    dialog.entries.setCurrentRow(1)
    dialog.forget_button.click()
    box = visible(dialog, QMessageBox)
    assert "stay until you delete their conversation" in box.informativeText()
    next(b for b in box.buttons() if b.text() == "Delete").click()
    await eventually(lambda: dialog.entries.count() == 1)
    assert [n.title for n in await h.runtime.notes()] == [PROFILE]
    window.close()


async def test_unsupported_memory_hides_memory_and_delete(make, qapp):
    h: Harness = await make(memory_kw={"editable": False})
    other = await h.chat()
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
    await eventually(lambda: str(default) in window.title_label.toolTip())
    assert window.folder_action.isVisible()

    window.folder_chosen(str(project))  # a new chat there; the unused one leaves nothing
    await eventually(lambda: controller.model.conversation.folder == str(project))
    await eventually(lambda: str(project) in window.title_label.toolTip())
    assert controller.model.conversation.id != empty.id
    assert await h.runtime.list_conversations() == []

    controller.send("hello")
    await eventually(lambda: widgets(window, AssistantBubble) and not controller.model.busy)
    talked = controller.model.conversation.id
    window.folder_chosen(str(other))  # a chat with messages stays; a new one starts
    await eventually(lambda: controller.model.conversation.folder == str(other))
    assert talked in [c.id for c in await h.runtime.list_conversations()]


async def test_the_folder_item_is_hidden_for_tools_with_a_fixed_folder(make, qapp):
    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)
    window._refresh_status()
    assert not window.folder_action.isVisible()


async def test_a_command_shows_its_description_with_the_command_behind_it(make, qapp):
    h: Harness = await make(permissions=PermissionPolicy.ASK)
    h.tools.instant("shell", read_only=False)
    h.core.add(Step(calls=[call("shell", {"command": "rm -rf build",
                                          "step_summary": "Remove the old build"})]),
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
    h.core.add(Step(calls=[call("look", {"q": "x", "step_summary": "Search the web for x"})]),
               Step(text="done"))
    await h.start()
    window, controller, _ = await open_window(h)
    controller.send("go")
    await eventually(lambda: not controller.model.busy and widgets(window, ToolCard))
    [card] = widgets(window, ToolCard)
    await eventually(lambda: card.title.text() == "Searched the web for x")
    assert not hasattr(card, "notes")


def test_replies_have_no_avatar_and_comfortable_lines(qapp):
    bubble = AssistantBubble(AssistantItem(text="One.\n\n- a\n- b\n\nTwo.", final=True))
    assert not [w for w in bubble.findChildren(QLabel) if w.objectName() == "avatar"]
    block = bubble.body.document().begin()
    # percent of the font's line spacing, itself about 1.35 em: about 1.6 em, not double
    assert 110 <= block.blockFormat().lineHeight() <= 125
    assert block.blockFormat().bottomMargin() >= 8


def test_inline_code_is_smaller_on_a_soft_background(qapp):
    bubble = AssistantBubble(AssistantItem(text="Run `uv run pytest` now.", final=True))
    fragments = bubble.body.document().begin().begin()
    formats = []
    while not fragments.atEnd():
        formats.append(fragments.fragment().charFormat())
        fragments += 1
    [code] = [f for f in formats if f.fontFixedPitch()]
    assert code.fontPointSize() < bubble.body.font().pointSizeF()
    assert code.background().style() != Qt.BrushStyle.NoBrush


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
    h.core.add(Step(calls=[call("look", {"q": 1, "step_summary": "Search the web"}),
                           call("open", {"url": "u", "step_summary": "Open the page"})]),
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
    h.core.add(Step(calls=[call("look", {"step_summary": "Search the web"})]),
               Step(calls=[call("wait", {"step_summary": "Wait for the build"})]))
    await h.start()
    window, controller, _ = await open_window(h)
    controller.send("go")
    await eventually(lambda: groups(window) and groups(window)[0].title.text()
                     == "Wait for the build" and groups(window)[0].state.text() == "running")
    (group,) = groups(window)  # "waiting" until the call starts, then "running"
    assert group.cancel.isVisibleTo(group)
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

    def settled() -> bool:  # past the redraw delay of the streamed bubble
        qapp.processEvents()
        shown = [b for b in window.findChildren(AssistantBubble) if b.isVisible()]
        return shown == widgets(window, AssistantBubble)

    await eventually(settled)
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


async def test_tool_servers_are_added_edited_and_removed(make, qapp, keychain):
    from kayden.frontends.desktop.window import McpDialog, ServerDialog

    h: Harness = await make(modules=ModuleState(running=RUNNING, work_folder="/start"))
    await h.start()
    window, _controller, _ = await open_window(h)
    window.show()  # dialogs and their confirmations need a visible parent
    window.open_tool_servers()  # the Tool servers page of Settings
    dialog = await eventually(lambda: window.mcp_dialog)
    assert isinstance(dialog, McpDialog) and dialog.isVisible()
    await eventually(lambda: dialog.empty.isVisible())
    dialog.add_button.click()
    server = dialog.findChild(ServerDialog)
    assert "trust" in server.warning.text()
    server.name.setText("github")
    server.target.setText("npx -y server-github")
    server.variables.setPlainText("GITHUB_TOKEN=ghp-1")
    server.save_button.click()
    await eventually(lambda: dialog.servers.count() == 1)
    assert keychain.store[("kayden", "mcp/github/GITHUB_TOKEN")] == "ghp-1"
    assert dialog.restart_button.isVisible()  # servers start with the next start
    dialog.servers.setCurrentRow(0)
    dialog.edit_button.click()
    server = [d for d in dialog.findChildren(ServerDialog) if d.isVisible()][-1]
    assert server.variables.toPlainText() == "GITHUB_TOKEN="  # the value stays hidden
    server.target.setText("not 'closed")
    server.save_button.click()
    await eventually(lambda: server.error.isVisible())
    assert "quotes" in server.error.text()
    server.reject()
    assert dialog.servers.currentRow() == 0 and dialog.remove_button.isEnabled()
    dialog.remove_button.click()
    [remove] = [b for b in message_box(dialog).buttons() if b.text() == "Remove"]
    remove.click()
    await eventually(lambda: dialog.servers.count() == 0)
    assert ("kayden", "mcp/github/GITHUB_TOKEN") not in keychain.store


async def test_a_server_list_read_before_a_save_does_not_replace_it(make, qapp, keychain):
    from kayden.api import McpServer
    from kayden.frontends.desktop.window import McpDialog

    h: Harness = await make(modules=ModuleState(running=RUNNING, work_folder="/start"))
    await h.start()
    window, controller, _ = await open_window(h)
    release = asyncio.Event()
    settings = h.runtime.settings

    async def slow_settings() -> Any:  # the window's first read answers late
        current = await settings()
        await release.wait()
        return current

    h.runtime.settings = slow_settings  # type: ignore[method-assign]
    dialog = McpDialog(window, h.runtime, controller)
    dialog.refresh()
    h.runtime.settings = settings  # type: ignore[method-assign]
    shown = await dialog.save([McpServer("docs", url="https://d.example/mcp")])
    dialog._show(shown)
    release.set()
    await asyncio.sleep(0.05)
    assert dialog.servers.count() == 1  # the late, older list was ignored


async def test_a_memory_update_after_an_answer_is_shown_with_a_link(make, qapp):
    from kayden.api import Completion
    from kayden.frontends.desktop.window import NoticeLine

    h: Harness = await make()

    async def maintain(complete: Completion) -> bool:
        conversation = h.runtime.snapshot().conversation
        if (await h.memory.history(conversation.id))[-1].role is not Role.ASSISTANT:
            return False
        h.store.save("Trip", "The Oslo trip", "Leaves Friday", None, conversation.id)
        return True

    h.memory.maintain_override = maintain
    h.core.add(Step(text="Have a good trip."))
    await h.start()
    window, controller, _ = await open_window(h)
    window.show()
    controller.send("I leave for Oslo on Friday")
    line = await eventually(lambda: next(
        (w for w in window._widgets.values() if isinstance(w, NoticeLine)), None))
    assert "Memory updated: Trip" in line.text() and "Open memory" in line.text()
    line.linkActivated.emit("memory")
    await eventually(lambda: window.memory_dialog is not None
                     and window.memory_dialog.isVisible())
    window.close()


async def test_the_duty_dialog_turns_a_duty_on_and_off(make, qapp):
    from kayden.api import Duty, DutyKind, PlanStatus
    from kayden.frontends.desktop.window import DutyDialog

    h: Harness = await make(budget=Budget(max_cost_usd=5))
    h.core.add(Step(text="on it"))
    await h.start()
    window, _controller, _ = await open_window(h)
    window.show()
    window.duty_action.trigger()
    [dialog] = window.findChildren(DutyDialog)
    assert dialog.save_button.text() == "Turn on"
    dialog.instruction.setPlainText("Keep the inbox answered")
    dialog.kind.setCurrentIndex(dialog.kind.findData(DutyKind.GOAL))
    dialog.max_minutes.setValue(30)
    dialog.plan.setPlainText("[doing] Read mail\nReply | due 2030-01-07 09:00")
    dialog.save_button.click()
    await eventually(lambda: h.runtime.snapshot().conversation.duty is not None)
    info = h.runtime.snapshot().conversation
    assert info.duty == Duty("Keep the inbox answered", DutyKind.GOAL, 60, 1800)
    assert [(i.text, i.status) for i in info.plan] == [
        ("Read mail", PlanStatus.DOING), ("Reply", PlanStatus.TODO)]
    await eventually(lambda: "Duty" in window.duty_label.text())
    window.open_duty()
    dialog = [d for d in window.findChildren(DutyDialog) if d.isVisible()][-1]
    [off] = buttons(dialog, "Turn off")
    off.click()
    await eventually(lambda: h.runtime.snapshot().conversation.duty is None)
    assert len(h.runtime.snapshot().conversation.plan) == 2  # the plan stays


async def test_the_duty_dialog_shows_why_it_cannot_save(make, qapp):
    from kayden.frontends.desktop.window import DutyDialog

    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)

    async def refuse(duty: object) -> None:
        raise ValueError("this duty cannot run")

    h.runtime.set_duty = refuse  # type: ignore[method-assign]
    window.show()
    window.open_duty()
    [dialog] = window.findChildren(DutyDialog)
    dialog.instruction.setPlainText("Watch")
    dialog.save_button.click()
    await eventually(lambda: dialog.error.isVisible())
    assert "cannot run" in dialog.error.text()
    assert dialog.isVisible() and dialog.save_button.isEnabled()


async def test_a_sub_session_sits_under_its_conversation_and_opens_in_place(
        make, qapp, tmp_path, child_env):
    from kayden.frontends.desktop.window import KIND_ROLE
    from tests.test_sub_sessions import finish, parent

    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Write a haiku"}, id="s")]),
        Step(text="Started."), Step(text="Got it.")],
        [finish("Haiku written."), {"text": "Here is the haiku."}, {"text": "Shorter now."}])
    controller = Controller(h.runtime, ConversationModel())
    window = MainWindow(h.runtime, controller, sessions=sessions)
    asyncio.create_task(controller.run())
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    window.show()
    await h.turn("Get me a haiku")
    await eventually(lambda: sessions.sessions and sessions.sessions[0].state == "done",
                     timeout=30)
    await window.fill_sidebar()
    kinds = [window.conversation_list.item(i).data(KIND_ROLE)
             for i in range(window.conversation_list.count())]
    assert kinds == ["group", "conversation", "session"]  # the project, then the tree
    row = window.conversation_list.item(2)
    assert row.text() == "Write a haiku" and "Done" in row.data(PREVIEW_ROLE)
    window._conversation_chosen(row)
    window._render_sidebar()  # the list changes while the child starts again
    current = window.conversation_list.currentItem()
    assert (current.data(KIND_ROLE), current.text()) == ("session", "Write a haiku")
    await eventually(lambda: window.controller is not window.home
                     and any(isinstance(i, AssistantItem) and "Here is the haiku." in i.text
                             for i in window.model.items), timeout=15)
    await asyncio.sleep(0.1)
    assert sessions._viewers[sessions.sessions[0].id] == 1
    current = window.conversation_list.currentItem()
    assert (current.data(KIND_ROLE), current.text()) == ("session", "Write a haiku")
    replies = sum(isinstance(i, AssistantItem) for i in window.model.items)
    window.input.setPlainText("Make it shorter")
    window._send()  # to the sub-session, started again on its conversation
    await eventually(lambda: sum(isinstance(i, AssistantItem) and bool(i.text)
                                 for i in window.model.items) > replies, timeout=15)
    assert any(getattr(i, "text", None) == "Make it shorter" for i in window.model.items)
    window._conversation_chosen(window.conversation_list.item(1))  # back to the parent
    assert window.controller is window.home
    assert window.model.conversation.id == h.conversation.id


async def test_projects_group_the_sidebar_and_fold(make, qapp, tmp_path):
    from kayden.frontends.desktop.window import KIND_ROLE

    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)
    one, two = tmp_path / "alpha", tmp_path / "beta"
    window._conversations = {
        "a": ConversationInfo("a", "First", 1.0, 3.0, folder=str(one)),
        "b": ConversationInfo("b", "Second", 1.0, 2.0, folder=str(two)),
        "c": ConversationInfo("c", "Third", 1.0, 1.0, folder=str(one))}
    window._render_sidebar()
    rows = [(window.conversation_list.item(i).data(KIND_ROLE),
             window.conversation_list.item(i).text())
            for i in range(window.conversation_list.count())]
    assert rows == [("group", "alpha"), ("conversation", "First"), ("conversation", "Third"),
                    ("group", "beta"), ("conversation", "Second")]
    window._conversation_chosen(window.conversation_list.item(0))  # fold alpha
    assert [window.conversation_list.item(i).text()
            for i in range(window.conversation_list.count())] == ["alpha", "beta", "Second"]


async def test_child_rows_have_a_menu(make, qapp, tmp_path, child_env):
    from kayden.frontends.desktop.window import KIND_ROLE
    from tests.test_sub_sessions import finish, parent

    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Tidy the notes"}, id="s")]),
        Step(text="Started."), Step(text="Thanks.")],
        [finish("Tidied."), {"text": "Done."}])
    controller = Controller(h.runtime, ConversationModel())
    window = MainWindow(h.runtime, controller, sessions=sessions)
    asyncio.create_task(controller.run())
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    window.show()
    await h.turn("Hi")
    [child] = sessions.sessions
    await eventually(lambda: child.state == "done", timeout=30)
    await window.fill_sidebar()
    menu = window.session_menu(child)
    assert [a.text() for a in menu.actions()] == [
        "Open", "Change spending limit…", "Rename…", "Archive", "Delete…"]
    sessions.rename(child, "Notes")
    assert any(window.conversation_list.item(i).text() == "Notes"
               and window.conversation_list.item(i).data(KIND_ROLE) == "session"
               for i in range(window.conversation_list.count()))


async def test_a_notice_from_a_child_wakes_the_open_conversation(make, qapp, tmp_path):
    from kayden.frontends.sessions import SubSessions

    h: Harness = await make()
    h.core.add(Step(text="Noted."))
    info = await h.start()
    sessions = SubSessions(tmp_path)
    controller = Controller(h.runtime, ConversationModel())
    window = MainWindow(h.runtime, controller, sessions=sessions)
    asyncio.create_task(controller.run())
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    assert window.home is controller
    sessions.notes[info.id] = ["[Runtime notice] Sub-session 1 (x) ended a turn: done."]
    sessions._notify()
    await eventually(lambda: info.id in h.store.conversations and any(
        "Sub-session 1 (x) ended a turn" in str(m.blocks)
        for m in h.store.conversations[info.id].messages))
    assert sessions.notes == {}


def test_long_call_details_are_clipped_until_shown(qapp):
    from kayden.frontends.desktop.window import Detail

    brief = "\n".join(f"Line {n} of a long sub-session brief." for n in range(12))
    detail = Detail(brief)
    assert detail.label.text().endswith("…") and "Line 5" not in detail.label.text()
    assert detail.toggle.isVisibleTo(detail) and detail.toggle.text() == "Show all"
    detail.toggle.click()
    assert detail.label.text() == brief and detail.toggle.text() == "Show less"
    detail.toggle.click()
    assert "Line 5" not in detail.label.text()
    short = Detail("ls -la")
    assert short.label.text() == "ls -la" and not short.toggle.isVisibleTo(short)


async def test_settings_set_the_limit_per_sub_session(make, qapp, tmp_path):
    from kayden.frontends.sessions import SubSessions

    h: Harness = await make()
    await h.start()
    window, controller, _ = await open_window(h)
    window.show()
    sessions = SubSessions(tmp_path)
    dialog = SettingsDialog(window, h.runtime, controller, await h.runtime.settings(), [], 0.0,
                            sessions)
    assert dialog.child_limit.text() == ""  # none, unless the user sets one
    assert dialog.child_limit.placeholderText() == "No limit"
    dialog.child_limit.setText("4")
    dialog.save_button.click()  # a limit where there was none: nothing to confirm
    await eventually(lambda: sessions.limit_usd == 4)
    assert SubSessions(tmp_path).limit_usd == 4
    dialog = SettingsDialog(window, h.runtime, controller, await h.runtime.settings(), [], 0.0,
                            sessions)
    assert dialog.child_limit.text() == "4"
    dialog.child_limit.setText("")
    dialog.save_button.click()  # removing it asks first, as raising does
    box = message_box(dialog)
    assert "removes the limit per sub-session" in box.informativeText()
    [save] = [b for b in box.buttons() if b.text() == "Save"]
    save.click()
    await eventually(lambda: sessions.limit_usd is None)
    assert SubSessions(tmp_path).limit_usd is None


async def test_a_long_notice_is_folded(make, qapp):
    from kayden.frontends.desktop.viewmodel import NoticeItem
    from kayden.frontends.desktop.window import Detail, NoticeLine

    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)
    brief = "New goal: Research Gen.G's path through Worlds.\n\nFind every series they played."
    assert isinstance(window._widget_for(NoticeItem(brief)), Detail)
    folded = window._widget_for(NoticeItem(brief))
    assert folded.label.text() == "New goal: Research Gen.G's path through Worlds.…"
    assert isinstance(window._widget_for(NoticeItem("Time to check in.")), NoticeLine)


async def test_the_menus_live_behind_two_buttons_and_their_shortcuts_still_work(make, qapp):
    import getpass

    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)
    assert window.menuBar().isHidden()
    texts = [a.text() for a in window.menu_button.menu().actions() if a.text()]
    assert texts == ["&New chat", "New chat in another &folder…", "&Duty…", "S&top", "&View",
                     "&Model"]
    profile = window.profile_button.menu()
    window._fill_profile_header()
    texts = [a.text() for a in profile.actions() if a.text() and a.isVisible()]
    assert texts[0].startswith(getpass.getuser())
    assert texts[1:4] == ["&Settings…", "&Memory…", "&Tool servers (MCP)…"]
    assert texts[-2:] == ["&Help", "&Quit"]
    shortcuts = {s.toString() for a in window.actions() for s in a.shortcuts()}
    assert {"Ctrl+N", "Ctrl+,", "Esc"} <= shortcuts
    window.gear_button.click()  # the click's `checked` is not a page name
    dialog = await eventually(lambda: window._dialog)
    await eventually(dialog.isVisible)
    assert dialog.stack.currentIndex() == 0


async def test_the_window_draws_its_own_title_bar(make, qapp):
    from PySide6.QtCore import QPoint

    from kayden.frontends.desktop.window import DragArea

    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert set(window.window_buttons) == {"minimize", "maximize", "close"}
    assert window.findChildren(DragArea)  # the header and the sidebar's top row
    window.resize(800, 600)
    window.show()
    edges = window._edges.edges
    assert edges(window.mapToGlobal(QPoint(2, 300))) == Qt.Edge.LeftEdge
    assert edges(window.mapToGlobal(QPoint(799, 599))) == (Qt.Edge.RightEdge
                                                          | Qt.Edge.BottomEdge)
    assert not edges(window.mapToGlobal(QPoint(400, 300)))
    window.window_buttons["maximize"].click()
    await eventually(lambda: window.isMaximized())
    assert window.window_buttons["maximize"].toolTip() == "Restore"
    assert not edges(window.mapToGlobal(QPoint(2, 300)))  # maximized: nothing to resize
    window.window_buttons["maximize"].click()
    await eventually(lambda: not window.isMaximized())


async def test_the_model_picker_under_the_message_box_shows_the_models_name(make, qapp):
    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)
    await window.refresh_models()
    assert window.model_button.text() == "Fake One"  # its name, not its id
    assert window.model_button.menu() is window.model_menu
    assert window.input.height() >= 2 * window.input.fontMetrics().lineSpacing()


async def test_the_permission_picker_tightens_at_once_and_asks_before_loosening(make, qapp):
    h: Harness = await make(permissions=PermissionPolicy.ALLOW_READ_ONLY)
    await h.start()
    window, _controller, _ = await open_window(h)
    window.show()
    window._refresh_status()
    assert window.permissions_button.text() == "Read only"
    window.choose_permissions(PermissionPolicy.ASK)  # stricter: no question
    await eventually(lambda: window.permissions_button.text() == "Ask first")
    assert (await h.runtime.settings()).permissions == PermissionPolicy.ASK
    window.choose_permissions(PermissionPolicy.AUTO)  # looser: asks first
    box = message_box(window)
    [allow] = [b for b in box.buttons() if b.text() == "Allow"]
    allow.click()
    await eventually(lambda: window.permissions_button.text() == "Auto")
    assert window.permission_actions[PermissionPolicy.AUTO].isChecked()


async def test_deleting_a_conversation_deletes_its_sub_sessions(make, qapp, tmp_path, child_env):
    from tests.test_sub_sessions import finish, parent

    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Write a haiku"}, id="s")]),
        Step(text="Started."), Step(text="Got it.")],
        [finish("Haiku written."), {"text": "Done."}])
    controller = Controller(h.runtime, ConversationModel())
    window = MainWindow(h.runtime, controller, sessions=sessions)
    asyncio.create_task(controller.run())
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    window.show()
    await h.turn("Go")
    [child] = sessions.sessions
    await eventually(lambda: child.state == "done", timeout=30)
    # the parent answers the child's report; a busy conversation cannot be deleted
    await eventually(lambda: len(h.core.requests) == 3
                     and h.runtime.snapshot().state == AgentState.IDLE, timeout=10)
    parent_info = h.conversation
    await window.fill_sidebar()
    window.delete_conversation(window.conversation_info(parent_info.id))
    box = message_box(window)
    assert "Its sub-session stops and is deleted too." in box.informativeText()
    [delete] = [b for b in box.buttons() if b.text() == "Delete"]
    delete.click()
    await eventually(lambda: not sessions.sessions, timeout=20)
    assert not child.data_dir.exists()


async def test_settings_has_sections_with_memory_and_tool_servers(make, qapp):
    h: Harness = await make()
    await h.start()
    window, controller, _ = await open_window(h)
    dialog = SettingsDialog(window, h.runtime, controller, await h.runtime.settings(), [], 0.0)
    assert [dialog.nav.item(i).text() for i in range(dialog.nav.count())] == [
        "General", "Spending", "Permissions", "Instructions", "Memory", "Tool servers"]
    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    dialog.show_page("Memory")
    assert dialog.stack.currentWidget() is dialog.memory
    dialog.show_page("Instructions")
    assert dialog.instructions.isVisibleTo(dialog.stack.currentWidget())


async def test_the_sidebar_starts_wide_enough_to_read_titles(make, qapp):
    from PySide6.QtWidgets import QSplitter

    h: Harness = await make()
    await h.start()
    window, _, _ = await open_window(h)
    window.resize(1400, 900)
    window.show()
    await asyncio.sleep(0.05)
    sidebar = window.findChild(QSplitter).sizes()[0]
    assert sidebar >= 320 * theme.zoom()
    window.close()


async def test_the_close_button_reaches_the_top_right_corner(make, qapp):
    from PySide6.QtCore import QPoint

    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)
    window.resize(1000, 700)
    window.show()
    await asyncio.sleep(0.05)
    corner = QPoint(window.width() - 1, 0)  # where a cursor pushed to the corner lands
    assert window.childAt(corner) is window.window_buttons["close"]
    for name, button in window.window_buttons.items():
        assert button.mapTo(window, QPoint(0, 0)).y() == 0, name  # each reaches the top
    window.close()


async def test_a_new_chat_stays_out_of_the_sidebar_until_its_first_message(make, qapp):
    h: Harness = await make()
    h.core.add(Step(text="hi"))
    await h.start()
    window, controller, _ = await open_window(h)
    await window.fill_sidebar()
    assert entries(window) == []
    controller.send("hello there")
    await eventually(lambda: entries(window) == ["hello there"])


async def test_leaving_an_unused_chat_leaves_nothing(make, qapp):
    h: Harness = await make()
    talked = await h.start()
    await h.turn("Pack for Oslo")
    window, controller, _ = await open_window(h)
    await window.fill_sidebar()
    window.new_action.trigger()
    await eventually(lambda: controller.model.conversation.id != talked.id)
    await window.fill_sidebar()
    assert entries(window) == ["Pack for Oslo"]
    window.open_conversation(talked.id)
    await eventually(lambda: controller.model.conversation.id == talked.id)
    assert [c.id for c in await h.runtime.list_conversations()] == [talked.id]


async def test_the_clicked_conversation_stays_highlighted_while_it_opens(make, qapp):
    h: Harness = await make()
    target = await h.chat("Target")
    await h.chat("Current")
    window, controller, _ = await open_window(h)
    await window.fill_sidebar()
    history = h.runtime.history

    async def slow_history(*args: Any, **kwargs: Any) -> Any:  # as SQLite, in a thread
        await asyncio.sleep(0.1)
        return await history(*args, **kwargs)

    h.runtime.history = slow_history  # type: ignore[method-assign]
    window.open_conversation(target.id)
    await asyncio.sleep(0.03)
    assert controller.model.conversation.id != target.id  # still opening
    window._render_sidebar()  # any refresh meanwhile: sub-sessions, a rename
    assert window.conversation_list.currentItem().text() == "Target"
    await eventually(lambda: controller.model.conversation.id == target.id)
    await window.fill_sidebar()
    assert window.conversation_list.currentItem().text() == "Target"


async def test_nothing_is_highlighted_while_a_new_chat_opens(make, qapp):
    h: Harness = await make()
    await h.chat("Current")
    window, _controller, _ = await open_window(h)
    await window.fill_sidebar()
    assert window.conversation_list.currentItem().text() == "Current"
    window.open_conversation(None)
    window._render_sidebar()
    assert window.conversation_list.currentItem() is None


async def test_one_click_switches_once_where_a_click_also_activates(make, qapp):
    h: Harness = await make()
    target = await h.chat("Target")
    await h.chat("Current")
    window, controller, _ = await open_window(h)
    await window.fill_sidebar()
    starts: list[Any] = []
    start = h.runtime.start

    async def counting(options: Any) -> Any:
        starts.append(options)
        return await start(options)

    h.runtime.start = counting  # type: ignore[method-assign]
    lst = window.conversation_list
    row = next(lst.item(i) for i in range(lst.count()) if lst.item(i).text() == "Target")
    lst.itemClicked.emit(row)
    lst.itemActivated.emit(row)  # KDE activates on a single click too
    await eventually(lambda: controller.model.conversation.id == target.id)
    await asyncio.sleep(0.05)
    assert len(starts) == 1


async def test_leaving_a_sub_session_for_its_parent_never_shows_the_old_chat(
        make, qapp, tmp_path, child_env):
    """Open chat A, a sub-session of chat B, then B: A must not light up on the way."""
    from PySide6.QtWidgets import QListWidgetItem

    from kayden.frontends.desktop.window import KIND_ROLE
    from tests.test_sub_sessions import finish, parent

    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Research"}, id="s")]),
        Step(text="Started."), Step(text="Noted.")], [finish("Done.")])
    await h.turn("Start research")
    parent_id = h.conversation.id
    await eventually(lambda: sessions.sessions and sessions.sessions[0].state == "done",
                     timeout=30)
    await eventually(lambda: len(h.core.requests) == 3  # the parent read the notice
                     and h.runtime.snapshot().state == AgentState.IDLE, timeout=15)
    await h.chat("Test message")  # now the open chat
    controller = Controller(h.runtime, ConversationModel())
    window = MainWindow(h.runtime, controller, sessions=sessions)
    asyncio.create_task(controller.run())
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    await window.fill_sidebar()
    lst = window.conversation_list

    def row(kind: str, match: Callable[[QListWidgetItem], bool]) -> QListWidgetItem:
        return next(lst.item(i) for i in range(lst.count())
                    if lst.item(i).data(KIND_ROLE) == kind and match(lst.item(i)))

    window._conversation_chosen(row("session", lambda r: True))
    await eventually(lambda: window._viewing is not None, timeout=15)
    highlighted: list[str] = []
    lst.currentItemChanged.connect(
        lambda now, _: now is not None and highlighted.append(now.text()))
    window._conversation_chosen(
        row("conversation", lambda r: r.data(Qt.ItemDataRole.UserRole) == parent_id))
    await eventually(lambda: controller.model.conversation.id == parent_id)
    await window.fill_sidebar()
    assert "Test message" not in highlighted


async def test_enter_opens_the_selected_conversation(make, qapp):
    h: Harness = await make()
    target = await h.chat("Target")
    await h.chat("Current")
    window, controller, _ = await open_window(h)
    await window.fill_sidebar()
    lst = window.conversation_list
    lst.setCurrentItem(next(lst.item(i) for i in range(lst.count())
                            if lst.item(i).text() == "Target"))
    QTest.keyClick(lst, Qt.Key.Key_Return)
    await eventually(lambda: controller.model.conversation.id == target.id)


async def test_the_last_click_wins_while_a_conversation_opens(make, qapp):
    h: Harness = await make()
    first = await h.chat("First")
    second = await h.chat("Second")
    await h.chat("Current")
    window, controller, _ = await open_window(h)
    await window.fill_sidebar()
    history = h.runtime.history

    async def slow_history(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.05)
        return await history(*args, **kwargs)

    h.runtime.history = slow_history  # type: ignore[method-assign]
    window.open_conversation(first.id)
    window.open_conversation(second.id)  # clicked before the first had opened
    window._render_sidebar()
    assert window.conversation_list.currentItem().text() == "Second"
    await eventually(lambda: controller.model.conversation.id == second.id
                     and window._opening is None)
    await asyncio.sleep(0.1)
    assert controller.model.conversation.id == second.id
    assert window.conversation_list.currentItem().text() == "Second"


async def test_a_failed_load_does_not_stop_later_switches(make, qapp):
    h: Harness = await make()
    first = await h.chat("First")
    await h.chat("Current")
    window, controller, _ = await open_window(h)
    await window.fill_sidebar()
    history = h.runtime.history
    failures = [KeyError("gone")]

    async def failing_once(*args: Any, **kwargs: Any) -> Any:
        if failures:
            raise failures.pop()
        return await history(*args, **kwargs)

    h.runtime.history = failing_once  # type: ignore[method-assign]
    window.open_conversation(first.id)
    await eventually(lambda: window._opening is None)
    window.open_conversation(None)
    await eventually(lambda: controller.model.conversation.id != first.id
                     and window._opening is None)


async def test_a_sub_session_still_opening_gives_way_to_a_later_click(
        make, qapp, tmp_path, child_env):
    from tests.test_sub_sessions import finish, parent

    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Research"}, id="s")]),
        Step(text="Started."), Step(text="Noted.")], [finish("Done.")])
    await h.turn("Start research")
    await eventually(lambda: sessions.sessions and sessions.sessions[0].state == "done"
                     and len(h.core.requests) == 3
                     and h.runtime.snapshot().state == AgentState.IDLE, timeout=30)
    parent_id = h.conversation.id
    await h.chat("Other")  # the open chat
    controller = Controller(h.runtime, ConversationModel())
    window = MainWindow(h.runtime, controller, sessions=sessions)
    asyncio.create_task(controller.run())
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    await window.fill_sidebar()
    child = sessions.sessions[0]
    opened = sessions.open

    async def slow_open(session: Any) -> Any:  # a stopped child starting again
        await asyncio.sleep(0.2)
        return await opened(session)

    sessions.open = slow_open  # type: ignore[method-assign]
    window.open_session(child)
    window.open_conversation(parent_id)  # changed their mind meanwhile
    await asyncio.sleep(0.5)
    assert window._viewing is None and window.controller is window.home
    # released, not left open (the child may still be starting on a busy machine)
    await eventually(lambda: sessions._viewers.get(child.id, 0) == 0)
    assert controller.model.conversation.id == parent_id
    assert window.conversation_list.currentItem().data(Qt.ItemDataRole.UserRole) == parent_id


async def test_a_new_chat_offers_its_folder_and_recent_ones(make, qapp, tmp_path):
    default, project, other = (tmp_path / n for n in ("default", "project", "other"))
    for folder in (default, project, other):
        folder.mkdir()
    tools = FakeTools()
    tools.folders = []
    h: Harness = await make(tools=tools, modules=ModuleState(work_folder=str(default)))
    h.core.add(Step(text="hi"))
    await h.chat("In project", folder=str(project))
    await h.chat("In other", folder=str(other))
    await h.start()  # a new chat, in the default folder
    window, controller, _ = await open_window(h)
    await window.fill_sidebar()
    window._refresh_status()
    button = window.folder_button
    assert not button.isHidden() and button.text().split()[0] == "default"
    assert button.toolTip().endswith(str(default))
    menu = button.menu()
    menu.aboutToShow.emit()
    labels = [a.text() for a in menu.actions() if a.text()]
    assert labels == ["default", "other", "project", "Choose folder…"]
    assert [a.text() for a in menu.actions() if a.isChecked()] == ["default"]
    next(a for a in menu.actions() if a.text() == "project").trigger()
    await eventually(lambda: controller.model.conversation.folder == str(project))
    await eventually(lambda: button.text().split()[0] == "project")
    controller.send("hello")
    await eventually(lambda: widgets(window, AssistantBubble) and not controller.model.busy)
    assert button.isHidden()  # chosen with the first message; the header shows it after


async def test_a_new_chat_shows_the_mascot_peeking(make, qapp):
    from PySide6.QtWidgets import QLabel

    h: Harness = await make()
    await h.start()
    window, _controller, _ = await open_window(h)
    [mascot] = [w for w in window.empty_state.findChildren(QLabel)
                if w.objectName() == "mascot"]
    assert not mascot.pixmap().isNull() and mascot.accessibleName() == "Kayden"


async def test_the_checklist_is_part_of_the_conversation(make, qapp):
    """It sits under the turn's newest message while the agent works, stays there when the
    turn ends, and scrolls away as the conversation goes on."""
    from kayden.frontends.desktop.viewmodel import ChecklistItem
    from kayden.frontends.desktop.window import PlanCard

    h: Harness = await make()
    plan = [{"text": "Read the code", "status": "done"},
            {"text": "Fix the bug", "status": "doing"}, {"text": "Run the tests"}]
    done = [{**step, "status": "done"} for step in plan]
    h.core.add(Step(calls=[call("update_plan", {"items": plan})]),
               Step(calls=[call("look", {"q": 1}), call("update_plan", {"items": done})]),
               Step(text="All fixed."), Step(text="You're welcome."))
    h.tools.instant("look", "found")
    await h.start()
    window, controller, _ = await open_window(h)
    controller.send("fix the bug")
    await eventually(lambda: not controller.model.busy and len(h.core.requests) == 3)
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    items = controller.model.items
    lists = [i for i in items if isinstance(i, ChecklistItem)]
    assert len(lists) == 1 and items[-1] is lists[0]  # one per turn, under its last message
    assert [str(s.status) for s in lists[0].steps] == ["done", "done", "done"]
    assert not lists[0].live  # the turn is over: it stays, at rest
    card = window._widgets[id(lists[0])]
    assert isinstance(card, PlanCard) and window.scroll.isAncestorOf(card)
    assert card.rows() == [("done", "Read the code"), ("done", "Fix the bug"),
                           ("done", "Run the tests")]
    controller.send("thanks")
    await eventually(lambda: not controller.model.busy and len(h.core.requests) == 4)
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    kinds = [type(i).__name__ for i in controller.model.items]
    assert kinds[-3:] == ["ChecklistItem", "UserItem", "AssistantItem"]  # it is history now


def test_a_reply_shows_copy_and_its_age_on_hover(qapp):
    import time

    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QEnterEvent

    bubble = AssistantBubble(AssistantItem(text="Hello **there**", final=True,
                                           created_at=time.time() - 125))
    assert not bubble.footer.isVisible() and bubble.footer.sizePolicy().retainSizeWhenHidden()
    bubble.show()
    QApplication.sendEvent(bubble, QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1)))
    assert bubble.footer.isVisible() and bubble.age.text() == "2 minutes ago"
    bubble.copy.click()
    assert QApplication.clipboard().text() == "Hello **there**"
    QApplication.sendEvent(bubble, QEvent(QEvent.Type.Leave))
    assert not bubble.footer.isVisible()


def test_relative_age_reads_like_a_person_would_say_it():
    from kayden.frontends.desktop.viewmodel import relative_age

    now = 1_000_000.0
    assert relative_age(now - 20, now) == "just now"
    assert relative_age(now - 60, now) == "1 minute ago"
    assert relative_age(now - 3 * 3600, now) == "3 hours ago"


async def test_sidebar_tooltips_show_only_the_full_title(make, qapp, tmp_path):
    from kayden.frontends.sessions import SubSession

    h: Harness = await make()
    talked = await h.start()
    await h.turn("A long first message that reads as the title of this conversation")
    window, _controller, _ = await open_window(h)
    session = SubSession("1", "Research the full bracket\n\nTips: lots of detail", talked.id,
                         tmp_path / "s1")

    class Sessions:
        listeners: list = []  # noqa: RUF012

        def of(self, conversation_id, include_archived=False):
            return (session,) if conversation_id == talked.id else ()

    window.sessions = Sessions()
    await window.fill_sidebar()
    lst = window.conversation_list
    tips = [lst.item(i).toolTip() for i in range(lst.count())]
    assert tips == ["A long first message that reads as the title of this conversation",
                    "Research the full bracket"]  # the brief's first line, not all of it


def test_a_long_message_wraps_inside_its_bubble(qapp):
    from kayden.frontends.desktop.viewmodel import UserItem

    text = ("ayo i jus added a planning tool like u can make a roadmap while working on "
            "some task and it shows as a checklist")
    bubble = UserBubble(UserItem(text, "m1"))
    bubble.resize(600, 200)
    bubble.show()
    qapp.processEvents()
    padding = bubble.bubble.layout().contentsMargins()
    room = bubble.bubble.width() - padding.left() - padding.right()
    assert bubble.text.width() <= room  # nothing cut at the right edge
    assert bubble.text.minimumWidth() <= room
    bubble.close()


async def test_a_line_says_kayden_is_thinking_before_anything_streams(make, qapp):
    from kayden.api import ReasoningDelta, StateChanged

    h = await make()
    await h.start()
    window, controller, _ = await open_window(h)
    assert window.waiting_line.isHidden()
    controller.model.apply(StateChanged(AgentState.THINKING))
    assert not window.waiting_line.isHidden() and window.waiting_line.text() == "Thinking…"
    controller.model.apply(ReasoningDelta("Looking at the files"))
    assert window.waiting_line.isHidden()  # the reply's own Thinking… line shows now
    controller.model.apply(StateChanged(AgentState.IDLE))
    assert window.waiting_line.isHidden()


def test_thinking_says_so_while_it_streams(qapp):
    item = AssistantItem(reasoning="Looking at the files")
    bubble = AssistantBubble(item)
    assert not bubble.thinking.isHidden() and bubble.thinking.text() == "Thinking…"
    item.text, item.final = "Done.", True
    bubble.refresh()
    assert bubble.thinking.text() == "Thinking"
