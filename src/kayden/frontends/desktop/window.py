"""Qt widgets for the desktop app. They render the view model and turn clicks into
Controller commands; no widget awaits the runtime.

Dialogs use open() and signals, never exec(): exec() would run a nested event loop inside
the qasync loop that also runs the runtime.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from kayden.api import (
    Decision,
    ModelInfo,
    PermissionPolicy,
    PermissionRequest,
    RuntimeAPI,
    Settings,
    SettingsUpdate,
)
from kayden.frontends.common import PERMISSION_LABELS, dollars, loosening, model_label
from kayden.frontends.desktop.viewmodel import (
    BUDGET_MESSAGE,
    AssistantItem,
    Change,
    Closed,
    Controller,
    ConversationModel,
    Inserted,
    Item,
    NoticeItem,
    PromptsChanged,
    Removed,
    Reset,
    SettingsForm,
    StatusChanged,
    ToolItem,
    Updated,
    UserItem,
    conversation_label,
    limit_update,
    needs_welcome,
    settings_update,
    summarize_arguments,
    user_message,
)

RENDER_INTERVAL_MS = 100  # Markdown of a streaming reply is re-rendered at most this often
DEFAULT_WORK_FOLDER = Path("~/Kayden")
KEY_PAGES = {
    "anthropic": "https://platform.claude.com/",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "openai": "https://platform.openai.com/api-keys",
}
DOCS_URL = "https://github.com/K5031/Kayden/blob/main/docs/desktop-app.md"
SEVERITY_COLORS = {"info": "palette(mid)", "warning": "#b26a00", "error": "#c62828"}


def _spawn(coro: Any) -> asyncio.Task[Any]:
    return asyncio.ensure_future(coro)


def _right(*widgets: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addStretch()
    for widget in widgets:
        row.addWidget(widget)
    return row


def _label(text: str = "", *, wrap: bool = True, selectable: bool = True) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(wrap)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


# ── transcript widgets ────────────────────────────────────────────────────


class MarkdownView(QTextBrowser):
    """Read-only Markdown that grows to fit its text instead of scrolling."""

    def __init__(self) -> None:
        super().__init__()
        self.setOpenExternalLinks(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.viewport().setAutoFillBackground(False)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)

    def _fit(self, *_: object) -> None:
        self.document().setTextWidth(self.viewport().width())
        self.setFixedHeight(int(self.document().size().height()) + 4)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._fit()


class UserBubble(QFrame):
    def __init__(self, item: UserItem) -> None:
        super().__init__()
        self.item = item
        self.setObjectName("user")
        layout = QVBoxLayout(self)
        self.text = _label()
        layout.addWidget(self.text)
        self.refresh()

    def refresh(self) -> None:
        self.text.setText(self.item.text + ("\n(queued)" if self.item.queued else ""))
        self.setEnabled(not self.item.queued)
        self.setAccessibleName("You" + (", queued" if self.item.queued else ""))


class AssistantBubble(QFrame):
    def __init__(self, item: AssistantItem) -> None:
        super().__init__()
        self.item = item
        self.setObjectName("assistant")
        layout = QVBoxLayout(self)
        self.thinking = QToolButton(text="Thinking", checkable=True)
        self.thinking.setArrowType(Qt.ArrowType.RightArrow)
        self.thinking.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.thinking.toggled.connect(self._toggle_thinking)
        self.reasoning = _label()
        self.reasoning.setEnabled(False)
        self.reasoning.hide()
        self.body = MarkdownView()
        self.body.setAccessibleName("Kayden")
        self.marker = _label(selectable=False)
        self.marker.setEnabled(False)
        for widget in (self.thinking, self.reasoning, self.body, self.marker):
            layout.addWidget(widget)
        self._timer = QTimer(self, singleShot=True, interval=RENDER_INTERVAL_MS)
        self._timer.timeout.connect(self._render)
        self._render()

    def refresh(self) -> None:
        if self.item.final or not self._timer.isActive():
            if self.item.final:
                self._timer.stop()
                self._render()
            else:
                self._timer.start()

    def _toggle_thinking(self, shown: bool) -> None:
        self.thinking.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)
        self.reasoning.setVisible(shown)

    def _render(self) -> None:
        item = self.item
        self.thinking.setVisible(bool(item.reasoning))
        self.reasoning.setText(item.reasoning)
        self.body.setVisible(bool(item.text))
        self.body.setMarkdown(item.text)
        marker = ("The model declined to answer." if item.refused
                  else "Interrupted before it finished." if item.interrupted else "")
        self.marker.setText(marker)
        self.marker.setVisible(bool(marker))


class ToolCard(QFrame):
    def __init__(self, item: ToolItem, cancel: Callable[[str], None]) -> None:
        super().__init__()
        self.item = item
        self.setObjectName("tool")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        head = QHBoxLayout()
        self.title = _label(wrap=False)
        self.state = _label(wrap=False, selectable=False)
        self.cancel = QPushButton("Cancel")
        self.cancel.setAccessibleName("Cancel this task")
        self.cancel.clicked.connect(lambda: cancel(self.item.call_id))
        self.toggle = QToolButton(text="Details", checkable=True)
        self.toggle.toggled.connect(self._toggle)
        head.addWidget(self.title, 1)
        head.addWidget(self.state)
        head.addWidget(self.cancel)
        head.addWidget(self.toggle)
        layout.addLayout(head)
        self.progress = _label()
        self.progress.setEnabled(False)
        self.notes = _label()
        self.notes.setEnabled(False)
        self.details = QPlainTextEdit(readOnly=True)
        self.details.setMaximumHeight(200)
        self.details.hide()
        for widget in (self.progress, self.notes, self.details):
            layout.addWidget(widget)
        self.refresh()

    def refresh(self) -> None:
        item = self.item
        self.title.setText(f"{item.name}  {item.summary}")
        duration = f" in {item.duration_s:.1f} s" if item.duration_s is not None else ""
        self.state.setText(item.state_label + duration)
        self.cancel.setVisible(item.cancellable)
        self.progress.setText(item.progress)
        self.progress.setVisible(bool(item.progress) and item.status is None)
        self.notes.setText("\n".join(item.notes))
        self.notes.setVisible(bool(item.notes))
        self.details.setPlainText(f"{item.arguments}\n\n{item.content}".strip())
        self.setAccessibleName(f"Tool {item.name}, {item.state_label}")

    def _toggle(self, shown: bool) -> None:
        self.details.setVisible(shown)


class NoticeLine(QLabel):
    def __init__(self, item: NoticeItem) -> None:
        super().__init__(item.text)
        self.item = item
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setStyleSheet(f"color: {SEVERITY_COLORS.get(item.severity, 'palette(text)')};")

    def refresh(self) -> None:
        self.setText(self.item.text)


class PermissionCard(QFrame):
    def __init__(self, request: PermissionRequest,
                 resolve: Callable[[str, Decision], None]) -> None:
        super().__init__()
        self.setObjectName("prompt")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        call = request.call
        kind = "It only reads." if request.read_only else "It may change things."
        text = _label(f"Kayden wants to use {call.name}. {kind}\n"
                      f"{summarize_arguments(call.arguments)}")
        layout.addWidget(text)
        buttons = QHBoxLayout()
        for decision, label in ((Decision.ALLOW_ONCE, "Allow once"),
                                (Decision.ALLOW_CONVERSATION, "Allow for this conversation"),
                                (Decision.DENY, "Deny")):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, d=decision: self._answer(request.id, d))
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.resolve = resolve
        self.setAccessibleName(f"Permission request for {call.name}")

    def _answer(self, request_id: str, decision: Decision) -> None:
        self.setEnabled(False)  # first-wins; the card goes when PermissionResolved arrives
        self.resolve(request_id, decision)


class MessageBox(QPlainTextEdit):
    """Enter sends; Shift+Enter starts a new line."""

    submitted = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if enter and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)


# ── main window ───────────────────────────────────────────────────────────


class MainWindow(QMainWindow):
    closed = Signal()

    def __init__(self, runtime: RuntimeAPI, controller: Controller) -> None:
        super().__init__()
        self.runtime = runtime
        self.controller = controller
        self.model: ConversationModel = controller.model
        self.model.on_change = self._changed
        self._widgets: dict[int, QWidget] = {}
        self._dialog: QDialog | None = None
        self.setWindowTitle("Kayden")
        self.resize(1000, 720)
        self._build()
        self._build_menus()
        self._refresh_status()

    # ── layout ────────────────────────────────────────────────────────────

    def _build(self) -> None:
        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        self.new_button = QPushButton("New conversation")
        self.new_button.clicked.connect(lambda: self.open_conversation(None))
        self.conversation_list = QListWidget()
        self.conversation_list.setAccessibleName("Conversations")
        self.conversation_list.itemActivated.connect(self._conversation_chosen)
        self.conversation_list.itemClicked.connect(self._conversation_chosen)
        self.switch_reason = _label(selectable=False)
        self.switch_reason.setEnabled(False)
        side.addWidget(self.new_button)
        side.addWidget(self.conversation_list, 1)
        side.addWidget(self.switch_reason)

        main = QWidget()
        column = QVBoxLayout(main)
        self.banner = QFrame(objectName="banner")
        banner = QHBoxLayout(self.banner)
        self.banner_text = _label()
        self.banner_settings = QPushButton("Open Settings")
        self.banner_settings.clicked.connect(self.open_settings)
        banner.addWidget(self.banner_text, 1)
        banner.addWidget(self.banner_settings)
        self.banner.hide()

        self.scroll = QScrollArea(widgetResizable=True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        outer = QVBoxLayout(content)
        self.load_earlier = QPushButton("Load earlier messages")
        self.load_earlier.clicked.connect(self._load_earlier)
        self.load_earlier.hide()
        self.items_layout = QVBoxLayout()
        self.items_layout.setSpacing(10)
        outer.addWidget(self.load_earlier)
        outer.addLayout(self.items_layout)
        outer.addStretch()
        self.scroll.setWidget(content)
        self._at_end = True
        bar = self.scroll.verticalScrollBar()
        bar.valueChanged.connect(lambda v: setattr(self, "_at_end", v >= bar.maximum() - 4))
        bar.rangeChanged.connect(lambda _lo, hi: self._at_end and bar.setValue(hi))

        self.prompts = QVBoxLayout()
        composer = QHBoxLayout()
        self.input = MessageBox()
        self.input.setPlaceholderText("Message Kayden (Enter to send, Shift+Enter for a new line)")
        self.input.setAccessibleName("Message")
        self.input.setMaximumHeight(110)
        self.input.submitted.connect(self._send)
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._send_or_stop)
        composer.addWidget(self.input, 1)
        composer.addWidget(self.send_button, 0, Qt.AlignmentFlag.AlignBottom)

        column.addWidget(self.banner)
        column.addWidget(self.scroll, 1)
        column.addLayout(self.prompts)
        column.addLayout(composer)

        splitter = QSplitter()
        splitter.addWidget(sidebar)
        splitter.addWidget(main)
        splitter.setSizes([240, 760])
        self.setCentralWidget(splitter)

        self.status_label = _label(wrap=False, selectable=False)
        self.usage_label = _label(wrap=False, selectable=False)
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.usage_label)
        self.setStyleSheet("""
            QFrame#user { background: palette(alternate-base); border-radius: 8px; }
            QFrame#banner { background: #fdecea; border-radius: 6px; }
            QFrame#banner QLabel { color: #8e1b1b; }
        """)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&Kayden")
        settings = QAction("&Settings…", self, shortcut=QKeySequence.StandardKey.Preferences)
        settings.triggered.connect(self.open_settings)
        stop = QAction("S&top", self, shortcut=QKeySequence("Esc"))
        stop.triggered.connect(self.controller.stop)
        quit_action = QAction("&Quit", self, shortcut=QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        for action in (settings, stop, quit_action):
            file_menu.addAction(action)
        self.model_menu = self.menuBar().addMenu("&Model")
        self.model_menu.aboutToShow.connect(lambda: _spawn(self._fill_model_menu()))

    # ── startup ───────────────────────────────────────────────────────────

    async def begin(self) -> None:
        """First run shows the welcome; otherwise resume the newest conversation."""
        settings = await self.runtime.settings()
        if needs_welcome(settings):
            self._dialog = WelcomeDialog(self, self.runtime, self.controller, settings)
            self._dialog.accepted.connect(lambda: self.open_conversation(None))
            self._dialog.open()
            return
        if self.runtime.snapshot().conversation is None:
            conversations = await self.controller.conversations()
            self.open_conversation(conversations[0].id if conversations else None)

    def open_conversation(self, conversation_id: str | None) -> None:
        if reason := self.controller.can_switch():
            self.model.notify(reason)
            return
        task = self.controller.open_conversation(conversation_id)
        task.add_done_callback(lambda _: (self.input.setFocus(), _spawn(self._fill_sidebar())))

    # ── view model changes ────────────────────────────────────────────────

    def _changed(self, change: Change) -> None:
        match change:
            case Reset():
                self._rebuild()
                _spawn(self._fill_sidebar())
            case Inserted(index=index, item=item):
                widget = self._widget_for(item)
                self._widgets[id(item)] = widget
                self.items_layout.insertWidget(index, widget)
            case Updated(item=item):
                if (widget := self._widgets.get(id(item))) is not None:
                    widget.refresh()  # type: ignore[attr-defined]
            case Removed(item=item):
                if (widget := self._widgets.pop(id(item), None)) is not None:
                    widget.deleteLater()
            case PromptsChanged():
                self._rebuild_prompts()
            case StatusChanged():
                self._refresh_status()
            case Closed():
                self.close()

    def _widget_for(self, item: Item) -> QWidget:
        match item:
            case UserItem():
                return UserBubble(item)
            case AssistantItem():
                return AssistantBubble(item)
            case ToolItem():
                return ToolCard(item, self.controller.cancel)
            case NoticeItem():
                return NoticeLine(item)

    def _rebuild(self) -> None:
        for widget in self._widgets.values():
            widget.deleteLater()
        self._widgets.clear()
        for index, item in enumerate(self.model.items):
            widget = self._widget_for(item)
            self._widgets[id(item)] = widget
            self.items_layout.insertWidget(index, widget)
        self._at_end = True
        self._rebuild_prompts()
        self._refresh_status()

    def _rebuild_prompts(self) -> None:
        while (child := self.prompts.takeAt(0)) is not None:
            if child.widget() is not None:
                child.widget().deleteLater()
        for request in self.model.prompts.values():
            self.prompts.addWidget(PermissionCard(request, self.controller.resolve))

    def _refresh_status(self) -> None:
        model = self.model
        self.status_label.setText(model.status_text)
        usage = " · ".join(p for p in (model.model or "", model.spending_text) if p)
        self.usage_label.setText(usage)
        self.send_button.setText("Stop" if model.busy else "Send")
        self.send_button.setAccessibleName("Stop" if model.busy else "Send message")
        self.load_earlier.setVisible(model.has_earlier)
        self.load_earlier.setEnabled(True)
        reason = self.controller.can_switch()
        self.new_button.setEnabled(reason is None)
        self.conversation_list.setEnabled(reason is None)
        self.switch_reason.setText(reason or "")
        self.switch_reason.setVisible(reason is not None)
        if (failure := model.failure) is not None:
            budget = self.controller.failure_is_budget
            self.banner_text.setText(BUDGET_MESSAGE if budget else failure.message)
            self.banner_settings.setVisible(budget)
        self.banner.setVisible(failure is not None)

    async def _fill_sidebar(self) -> None:
        try:
            conversations = await self.controller.conversations()
        except Exception as e:  # noqa: BLE001 - shown, never fatal
            self.model.notify(user_message(e))
            return
        current = self.model.conversation.id if self.model.conversation else None
        self.conversation_list.clear()
        for info in conversations:
            entry = QListWidgetItem(conversation_label(info))
            entry.setData(Qt.ItemDataRole.UserRole, info.id)
            self.conversation_list.addItem(entry)
            if info.id == current:
                self.conversation_list.setCurrentItem(entry)

    def _conversation_chosen(self, entry: QListWidgetItem) -> None:
        conversation_id = entry.data(Qt.ItemDataRole.UserRole)
        if self.model.conversation is None or conversation_id != self.model.conversation.id:
            self.open_conversation(conversation_id)

    # ── commands ──────────────────────────────────────────────────────────

    def _send(self) -> None:
        text = self.input.toPlainText().strip()
        if text:
            self.input.clear()
            self.controller.send(text)

    def _send_or_stop(self) -> None:
        if self.model.busy:
            self.controller.stop()
        else:
            self._send()

    def _load_earlier(self) -> None:
        self.load_earlier.setEnabled(False)
        self._at_end = False
        self.controller.load_earlier()

    async def _fill_model_menu(self) -> None:
        menu = self.model_menu
        menu.clear()
        try:
            models = await self.runtime.list_models()
        except Exception as e:  # noqa: BLE001
            menu.addAction(user_message(e)).setEnabled(False)
            return
        for info in models:
            action = menu.addAction(model_label(info))
            action.setCheckable(True)
            action.setChecked(info.id == self.model.model)
            action.triggered.connect(lambda _=False, m=info.id: self.controller.select_model(m))

    def open_settings(self) -> None:
        async def load() -> None:
            settings = await self.runtime.settings()
            try:
                models = await self.runtime.list_models()
            except Exception:  # noqa: BLE001 - e.g. no key yet; the provider can still change
                models = []
            spent = self.runtime.snapshot().usage.usage.cost_usd or 0.0
            self._dialog = SettingsDialog(self, self.runtime, self.controller, settings, models,
                                          spent)
            self._dialog.open()

        self.controller.command(load())

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closed.emit()
        super().closeEvent(event)


# ── settings ──────────────────────────────────────────────────────────────


def confirm(parent: QWidget, text: str, then: Callable[[], None]) -> QMessageBox:
    box = QMessageBox(QMessageBox.Icon.Warning, "Kayden", text,
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    box.finished.connect(lambda result: result == QMessageBox.StandardButton.Yes and then())
    box.open()
    return box


def choose_folder(parent: QWidget, start: str, then: Callable[[str], None]) -> QFileDialog:
    dialog = QFileDialog(parent, "Choose the work folder", start)
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly)
    dialog.fileSelected.connect(then)
    dialog.open()
    return dialog


class SettingsDialog(QDialog):
    """Sends only what changed; loosening is confirmed first."""

    def __init__(self, parent: QWidget, runtime: RuntimeAPI, controller: Controller,
                 settings: Settings, models: Sequence[ModelInfo], spent: float) -> None:
        super().__init__(parent)
        self.runtime, self.controller, self.settings = runtime, controller, settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        form = QFormLayout(self)

        self.provider = QComboBox()
        for p in settings.providers:
            saved = "" if p.key_name is None else ("  (key saved)" if p.has_key
                                                    else "  (no key yet)")
            self.provider.addItem(p.name + saved, p.id)
        if settings.provider is None:
            self.provider.insertItem(0, "Custom (from config.toml)", None)
        self.provider.setCurrentIndex(max(0, self.provider.findData(settings.provider)))
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.api_key = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Leave empty to keep the current key")
        self.model = QComboBox()
        self.light = QComboBox()
        self.light.addItem("Same as the main model", None)
        for info in models:
            self.model.addItem(model_label(info), info.id)
            self.light.addItem(model_label(info), info.id)
        if self.model.findData(settings.model) < 0:
            self.model.insertItem(0, settings.model or "Provider's default", settings.model)
        self.model.setCurrentIndex(self.model.findData(settings.model))
        self.light.setCurrentIndex(max(0, self.light.findData(settings.maintenance_model)))
        self.model_note = _label("A new provider starts with its first model; change it "
                                 "after saving.", selectable=False)
        self.model_note.hide()
        self.limit = QLineEdit("" if settings.budget.max_cost_usd is None
                               else f"{settings.budget.max_cost_usd:g}")
        self.limit.setPlaceholderText("No limit")
        self.permissions = QComboBox()
        for policy, label in PERMISSION_LABELS.items():
            self.permissions.addItem(label, policy)
        self.permissions.setCurrentIndex(self.permissions.findData(settings.permissions))
        self.instructions = QPlainTextEdit(settings.system_prompt)
        self.instructions.setMinimumHeight(120)
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(settings.work_folder or "")
        browse = QPushButton("Choose…")
        browse.clicked.connect(lambda: choose_folder(self, self.folder.text(),
                                                     self.folder.setText))
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        for widget in (self.folder, browse):
            widget.setEnabled(settings.work_folder is not None)
        self.error = _label()
        self.error.setStyleSheet(f"color: {SEVERITY_COLORS['error']};")
        self.error.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)

        form.addRow("AI provider", self.provider)
        form.addRow("API key", self.api_key)
        form.addRow("Model", self.model)
        form.addRow("Model for memory upkeep", self.light)
        form.addRow("", self.model_note)
        form.addRow("Spending limit (US$)", self.limit)
        form.addRow("", _label(f"{dollars(spent)} spent so far", selectable=False))
        form.addRow("What the agent may do", self.permissions)
        form.addRow("Instructions", self.instructions)
        form.addRow("Work folder", folder_row)
        form.addRow(self.error)
        form.addRow(buttons)

    def _provider_changed(self) -> None:
        switching = self.provider.currentData() != self.settings.provider
        self.model.setEnabled(not switching)
        self.light.setEnabled(not switching)
        self.model_note.setVisible(switching)

    def form(self) -> SettingsForm:
        return SettingsForm(
            provider=self.provider.currentData(),
            api_key=self.api_key.text(),
            model=self.model.currentData(),
            maintenance_model=self.light.currentData(),
            spending_limit=self.limit.text(),
            permissions=PermissionPolicy(self.permissions.currentData()),
            instructions=self.instructions.toPlainText(),
            work_folder=self.folder.text() if self.folder.isEnabled() else None,
        )

    def _save(self) -> None:
        try:
            update = settings_update(self.settings, self.form())
        except ValueError as e:
            self._show_error(str(e))
            return
        if update == SettingsUpdate():
            self.accept()
            return
        if reasons := loosening(self.settings, update):
            confirm(self, "This change " + " and ".join(reasons) + ". Continue?",
                    lambda: self._apply(update))
        else:
            self._apply(update)

    def _apply(self, update: SettingsUpdate) -> None:
        self.save_button.setEnabled(False)
        self.error.hide()

        def failed(error: Exception) -> None:
            self.save_button.setEnabled(True)
            self._show_error(user_message(error))

        self.controller.command(self.runtime.update_settings(update),
                                done=lambda _: self.accept(), failed=failed)

    def _show_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()


# ── first run ─────────────────────────────────────────────────────────────


class WelcomeDialog(QDialog):
    """Three pages: provider, key, then work folder and spending limit. Accepted when
    everything is saved; the window then starts a conversation."""

    def __init__(self, parent: QWidget, runtime: RuntimeAPI, controller: Controller,
                 settings: Settings) -> None:
        super().__init__(parent)
        self.runtime, self.controller, self.settings = runtime, controller, settings
        self.setWindowTitle("Welcome to Kayden")
        self.setMinimumWidth(520)
        self.pages = QStackedWidget()
        layout = QVBoxLayout(self)
        layout.addWidget(self.pages)

        # 1. provider
        page = QWidget()
        column = QVBoxLayout(page)
        column.addWidget(_label("Choose your AI provider. Kayden uses its models and you pay "
                                "it directly for what you use."))
        self.provider = QListWidget()
        self.provider.setAccessibleName("AI provider")
        for p in settings.providers:
            entry = QListWidgetItem(p.name)
            entry.setData(Qt.ItemDataRole.UserRole, p.id)
            self.provider.addItem(entry)
        self.provider.setCurrentRow(max(0, next(
            (i for i, p in enumerate(settings.providers) if p.id == settings.provider), 0)))
        column.addWidget(self.provider)
        next_button = QPushButton("Continue")
        next_button.setDefault(True)
        next_button.clicked.connect(lambda: self.pages.setCurrentIndex(1))
        column.addLayout(_right(next_button))
        self.pages.addWidget(page)

        # 2. key
        page = QWidget()
        column = QVBoxLayout(page)
        self.key_intro = _label()
        get_key = QPushButton("Get a key")
        get_key.clicked.connect(self._open_key_page)
        self.key = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Paste your API key")
        self.key.setAccessibleName("API key")
        self.key_error = _label()
        self.key_error.setStyleSheet(f"color: {SEVERITY_COLORS['error']};")
        self.key_error.hide()
        column.addWidget(self.key_intro)
        column.addWidget(get_key, 0, Qt.AlignmentFlag.AlignLeft)
        column.addWidget(self.key)
        column.addWidget(self.key_error)
        column.addStretch()
        self.key_continue = QPushButton("Check and save the key")
        self.key_continue.clicked.connect(self._save_key)
        back = QPushButton("Back")
        back.clicked.connect(lambda: self.pages.setCurrentIndex(0))
        row = QHBoxLayout()
        row.addWidget(back)
        row.addStretch()
        row.addWidget(self.key_continue)
        column.addLayout(row)
        self.pages.addWidget(page)
        self.pages.currentChanged.connect(self._page_shown)

        # 3. folder and limit
        page = QWidget()
        column = QVBoxLayout(page)
        column.addWidget(_label("Kayden reads and writes files only inside its work folder."))
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(settings.work_folder or str(DEFAULT_WORK_FOLDER.expanduser()))
        self.folder.setEnabled(settings.work_folder is not None)
        browse = QPushButton("Choose…")
        browse.setEnabled(settings.work_folder is not None)
        browse.clicked.connect(lambda: choose_folder(self, self.folder.text(),
                                                     self.folder.setText))
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        column.addLayout(folder_row)
        column.addWidget(_label("Spending limit in US dollars (optional). Kayden stops when it "
                                "is reached."))
        self.limit = QLineEdit()
        self.limit.setPlaceholderText("No limit")
        column.addWidget(self.limit)
        column.addWidget(_label("Kayden asks before every action. You can change that, and "
                                "everything here, in Settings."))
        self.finish_error = _label()
        self.finish_error.setStyleSheet(f"color: {SEVERITY_COLORS['error']};")
        self.finish_error.hide()
        column.addWidget(self.finish_error)
        column.addStretch()
        self.finish_button = QPushButton("Start using Kayden")
        self.finish_button.clicked.connect(self._finish)
        column.addLayout(_right(self.finish_button))
        self.pages.addWidget(page)

    def _chosen(self) -> Any:
        provider_id = self.provider.currentItem().data(Qt.ItemDataRole.UserRole)
        return next(p for p in self.settings.providers if p.id == provider_id)

    def _page_shown(self, index: int) -> None:
        if index == 1:
            self.key_intro.setText(f"Paste your {self._chosen().name} API key. Kayden keeps it "
                                   "in your computer's keychain.")
            self.key.setFocus()

    def _open_key_page(self) -> None:
        if url := KEY_PAGES.get(self._chosen().id):
            QDesktopServices.openUrl(QUrl(url))

    def _save_key(self) -> None:
        key = self.key.text().strip()
        if not key:
            self._error(self.key_error, "Paste a key first.")
            return
        provider = self._chosen()
        update = SettingsUpdate(api_key=key, provider=None if provider.id == self.settings.provider
                                else provider.id)
        self.key_continue.setEnabled(False)
        self.key_error.hide()

        def failed(error: Exception) -> None:
            self.key_continue.setEnabled(True)
            hint = (f"\nIf your computer has no keychain, set the environment variable "
                    f"{provider.key_name} to the key instead (see {DOCS_URL})."
                    if provider.key_name else "")
            self._error(self.key_error, user_message(error) + hint)

        def saved(settings: Settings) -> None:
            self.settings = settings
            self.pages.setCurrentIndex(2)

        self.controller.command(self.runtime.update_settings(update), done=saved, failed=failed)

    def _finish(self) -> None:
        changes: dict[str, Any] = {}
        folder = self.folder.text().strip()
        if self.folder.isEnabled() and folder and folder != self.settings.work_folder:
            Path(folder).expanduser().mkdir(parents=True, exist_ok=True)
            changes["work_folder"] = str(Path(folder).expanduser())
        try:
            budget = limit_update(self.settings, self.limit.text())
        except ValueError as e:
            self._error(self.finish_error, str(e))
            return
        if budget != self.settings.budget:
            changes["budget"] = budget  # a first limit only tightens: no confirmation
        if not changes:
            self.accept()
            return
        self.finish_button.setEnabled(False)

        def failed(error: Exception) -> None:
            self.finish_button.setEnabled(True)
            self._error(self.finish_error, user_message(error))

        self.controller.command(self.runtime.update_settings(SettingsUpdate(**changes)),
                                done=lambda _: self.accept(), failed=failed)

    @staticmethod
    def _error(label: QLabel, text: str) -> None:
        label.setText(text)
        label.show()

