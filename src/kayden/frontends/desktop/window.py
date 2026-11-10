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

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QContextMenuEvent,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from kayden.api import (
    ConversationInfo,
    Decision,
    MemoryEntry,
    ModelInfo,
    PermissionPolicy,
    PermissionRequest,
    RuntimeAPI,
    Settings,
    SettingsUpdate,
    ToolStatus,
)
from kayden.frontends.common import (
    AT_RESTART,
    MEMORY_WARNING,
    MODULE_LABELS,
    PERMISSION_LABELS,
    delete_details,
    dollars,
    loosening,
    model_label,
    module_options,
    needs_restart,
    restart_notice,
)
from kayden.frontends.desktop import app_menu, theme
from kayden.frontends.desktop.viewmodel import (
    BUDGET_MESSAGE,
    AssistantItem,
    Change,
    Closed,
    Controller,
    ConversationListChanged,
    ConversationModel,
    Inserted,
    Item,
    MemoryListChanged,
    NoticeItem,
    PromptsChanged,
    Removed,
    Reset,
    SettingsForm,
    StatusChanged,
    ToolItem,
    Updated,
    UserItem,
    clip,
    conversation_label,
    conversation_preview,
    limit_update,
    memory_source,
    needs_welcome,
    relative_date,
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
EMPTY_TITLE = "Start a conversation"
EMPTY_HINT = "Ask a question or describe a task."
BUBBLE_SHARE = 0.75  # a message of yours takes at most this share of the transcript's width
TOOL_TONES = {ToolStatus.OK: "ok", ToolStatus.ERROR: "error", ToolStatus.ABANDONED: "warning",
              ToolStatus.TIMED_OUT: "warning", ToolStatus.INTERRUPTED: "warning"}
# Item data the sidebar and memory rows are drawn from.
TITLE_ROLE = Qt.ItemDataRole.UserRole + 1
PREVIEW_ROLE = Qt.ItemDataRole.UserRole + 2
DATE_ROLE = Qt.ItemDataRole.UserRole + 3
FLAGS_ROLE = Qt.ItemDataRole.UserRole + 4  # {"archived"}


def _spawn(coro: Any) -> asyncio.Task[Any]:
    return asyncio.ensure_future(coro)


def _right(*widgets: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addStretch()
    for widget in widgets:
        row.addWidget(widget)
    return row


def _label(text: str = "", *, wrap: bool = True, selectable: bool = True,
           tone: str | None = None, role: str | None = None) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(wrap)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setProperty("tone", tone)
    label.setProperty("role", role)
    return label


def _primary(button: QPushButton) -> QPushButton:
    button.setProperty("primary", True)
    return button


class ElidedLabel(QLabel):
    """One line that ends in "…" instead of widening its window."""

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight,
                                             self.contentsRect().width())
        painter.drawText(self.contentsRect(), Qt.AlignmentFlag.AlignVCenter, text)


class Switch(QCheckBox):
    """A check box drawn as a sliding switch."""

    TRACK = QSize(30, 18)
    GAP = 8

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        return QSize(self.TRACK.width() + self.GAP + metrics.horizontalAdvance(self.text()) + 2,
                     max(self.TRACK.height(), metrics.height()) + 4)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def hitButton(self, pos: QPoint) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, event: Any) -> None:
        colors = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.TRACK.width(), self.TRACK.height()
        track = QRectF(0.5, (self.height() - height) / 2, width, height)
        fill = QColor(colors.accent if self.isChecked() else colors.track)
        if not self.isEnabled():
            fill.setAlphaF(0.45)
        painter.setPen(QPen(QColor(colors.accent), 2) if self.hasFocus()
                       and self.focusPolicy() != Qt.FocusPolicy.NoFocus
                       and self.testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange)
                       else Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(track, height / 2, height / 2)
        knob = height - 4
        x = track.x() + (width - knob - 2 if self.isChecked() else 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QRectF(x, track.y() + 2, knob, knob))
        painter.setPen(QColor(colors.text if self.isEnabled() else colors.muted))
        painter.drawText(QRectF(width + self.GAP, 0, self.width() - width - self.GAP,
                                self.height()), Qt.AlignmentFlag.AlignVCenter, self.text())


class RowDelegate(QStyledItemDelegate):
    """Draws a list row as a title, an optional muted second line and a date on the right.
    With `wrap`, the title wraps instead of ending in "…"."""

    PAD = QSize(10, 7)

    def __init__(self, view: QListWidget, *, wrap: bool = False) -> None:
        super().__init__(view)
        self.view, self.wrap = view, wrap

    def _texts(self, index: Any) -> tuple[str, str, str, set[str]]:
        title = index.data(TITLE_ROLE) or index.data(Qt.ItemDataRole.DisplayRole) or ""
        return (title, index.data(PREVIEW_ROLE) or "", index.data(DATE_ROLE) or "",
                index.data(FLAGS_ROLE) or set())

    def _title_font(self, option: QStyleOptionViewItem) -> QFont:
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold if not self.wrap else QFont.Weight.Normal)
        return font

    def _small_font(self, option: QStyleOptionViewItem) -> QFont:
        font = QFont(option.font)
        font.setPointSizeF(font.pointSizeF() * 0.9)
        return font

    def _title_height(self, option: QStyleOptionViewItem, title: str, width: int) -> int:
        metrics = QFontMetrics(self._title_font(option))
        if not self.wrap:
            return metrics.height()
        flags = Qt.TextFlag.TextWordWrap
        return metrics.boundingRect(0, 0, max(width, 40), 10_000, flags, title).height()

    def sizeHint(self, option: QStyleOptionViewItem, index: Any) -> QSize:
        title, preview, _, _ = self._texts(index)
        width = self.view.viewport().width() - 2 * self.PAD.width()
        height = self._title_height(option, title, width)
        if preview:
            height += 2 + QFontMetrics(self._small_font(option)).height()
        return QSize(width, height + 2 * self.PAD.height() + 2)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Any) -> None:
        colors = theme.current()
        title, preview, date, flags = self._texts(index)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(option.rect).adjusted(0, 1, 0, -1)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colors.accent_soft if selected else colors.card))
            painter.drawRoundedRect(box, 8, 8)
        inner = option.rect.adjusted(self.PAD.width(), self.PAD.height() + 1,
                                     -self.PAD.width(), -self.PAD.height() - 1)
        small = self._small_font(option)
        small_metrics = QFontMetrics(small)
        left = inner.left()
        right = inner.right()
        if date:
            painter.setFont(small)
            painter.setPen(QColor(colors.muted))
            date_width = small_metrics.horizontalAdvance(date)
            painter.drawText(right - date_width, inner.top(), date_width + 1,
                             QFontMetrics(self._title_font(option)).height(),
                             Qt.AlignmentFlag.AlignVCenter, date)
            right -= date_width + 8
        title_font = self._title_font(option)
        painter.setFont(title_font)
        muted = "archived" in flags
        painter.setPen(QColor(colors.accent_text if selected else
                              colors.muted if muted else colors.text))
        title_height = self._title_height(option, title, right - left)
        if self.wrap:
            painter.drawText(left, inner.top(), right - left, title_height,
                             Qt.TextFlag.TextWordWrap, title)
        else:
            painter.drawText(left, inner.top(), right - left, title_height,
                             Qt.AlignmentFlag.AlignVCenter,
                             QFontMetrics(title_font).elidedText(
                                 title, Qt.TextElideMode.ElideRight, right - left))
        if preview:
            painter.setFont(small)
            painter.setPen(QColor(colors.muted))
            painter.drawText(inner.left(), inner.top() + title_height + 2, inner.width(),
                             small_metrics.height(), Qt.AlignmentFlag.AlignVCenter,
                             small_metrics.elidedText(preview, Qt.TextElideMode.ElideRight,
                                                      inner.width()))
        painter.restore()


# ── transcript widgets ────────────────────────────────────────────────────


class MarkdownView(QTextBrowser):
    """Read-only Markdown that grows to fit its text instead of scrolling."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("reply")
        self.setOpenExternalLinks(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.viewport().setAutoFillBackground(False)
        self.document().setDocumentMargin(0)
        self.document().setIndentWidth(20)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)

    def _fit(self, *_: object) -> None:
        self.document().setTextWidth(self.viewport().width())
        self.setFixedHeight(int(self.document().size().height()) + 4)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._fit()


def _avatar(size: int = 24, name: str = "avatar") -> QLabel:
    avatar = QLabel("K", objectName=name)
    avatar.setFixedSize(size, size)
    avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
    avatar.setAccessibleName("Kayden")
    return avatar


class UserBubble(QWidget):
    """Your message: an accent bubble on the right."""

    def __init__(self, item: UserItem) -> None:
        super().__init__()
        self.item = item
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        self.bubble = QFrame(objectName="user")
        layout = QVBoxLayout(self.bubble)
        layout.setContentsMargins(12, 8, 12, 8)
        self.text = _label()
        layout.addWidget(self.text)
        row.addWidget(self.bubble)
        self.refresh()

    def refresh(self) -> None:
        self.text.setText(self.item.text + ("\n(queued)" if self.item.queued else ""))
        self.setEnabled(not self.item.queued)
        self.setAccessibleName("You" + (", queued" if self.item.queued else ""))
        self._fit()

    def _fit(self) -> None:
        """As wide as its longest line, up to BUBBLE_SHARE of the transcript."""
        widest = int(self.width() * BUBBLE_SHARE)
        margins = self.bubble.contentsMargins()
        natural = max(self.text.fontMetrics().horizontalAdvance(line)
                      for line in self.text.text().split("\n")) + 2
        self.text.setMinimumWidth(min(natural, widest - margins.left() - margins.right()))
        self.bubble.setMaximumWidth(widest)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._fit()


class AssistantBubble(QWidget):
    """Kayden's reply: an avatar, then the text with no bubble, easy to read when long."""

    def __init__(self, item: AssistantItem) -> None:
        super().__init__()
        self.item = item
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(_avatar(), 0, Qt.AlignmentFlag.AlignTop)
        layout = QVBoxLayout()
        layout.setSpacing(4)
        row.addLayout(layout, 1)
        self.thinking = QToolButton(text="Thinking", checkable=True)
        self.thinking.setArrowType(Qt.ArrowType.RightArrow)
        self.thinking.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.thinking.toggled.connect(self._toggle_thinking)
        self.reasoning = _label(tone="muted")
        self.reasoning.hide()
        self.body = MarkdownView()
        self.body.setAccessibleName("Kayden")
        self.marker = _label(selectable=False, tone="muted")
        layout.addWidget(self.thinking, 0, Qt.AlignmentFlag.AlignLeft)
        for widget in (self.reasoning, self.body, self.marker):
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
    """One row: the tool, what it works on, its state; details unfold below."""

    def __init__(self, item: ToolItem, cancel: Callable[[str], None]) -> None:
        super().__init__()
        self.item = item
        self.setObjectName("tool")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 6, 6, 6)
        layout.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.name = _label(wrap=False, selectable=False, role="strong")
        self.title = ElidedLabel()
        self.title.setProperty("tone", "muted")
        self.state = _label(wrap=False, selectable=False)
        self.cancel = QPushButton("Cancel")
        self.cancel.setAccessibleName("Cancel this task")
        self.cancel.clicked.connect(lambda: cancel(self.item.call_id))
        self.toggle = QToolButton(checkable=True)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow)
        self.toggle.setToolTip("Details")
        self.toggle.setAccessibleName("Details")
        self.toggle.toggled.connect(self._toggle)
        head.addWidget(self.name)
        head.addWidget(self.title, 1)
        head.addWidget(self.state)
        head.addWidget(self.cancel)
        head.addWidget(self.toggle)
        layout.addLayout(head)
        self.progress = _label(tone="muted")
        self.notes = _label(tone="muted")
        self.details = QPlainTextEdit(readOnly=True)
        self.details.setMaximumHeight(200)
        self.details.hide()
        for widget in (self.progress, self.notes, self.details):
            layout.addWidget(widget)
        self.refresh()

    def refresh(self) -> None:
        item = self.item
        self.name.setText(item.name)
        self.title.setText(item.summary)
        self.title.setToolTip(item.summary)
        duration = f" in {item.duration_s:.1f} s" if item.duration_s is not None else ""
        self.state.setText(item.state_label + duration)
        theme.mark(self.state, "tone", TOOL_TONES.get(item.status, "muted")
                   if item.status is not None else "muted")
        self.cancel.setVisible(item.cancellable)
        self.progress.setText(item.progress)
        self.progress.setVisible(bool(item.progress) and item.status is None)
        self.notes.setText("\n".join(item.notes))
        self.notes.setVisible(bool(item.notes))
        self.details.setPlainText(f"{item.arguments}\n\n{item.content}".strip())
        self.setAccessibleName(f"Tool {item.name}, {item.state_label}")

    def _toggle(self, shown: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.UpArrow if shown else Qt.ArrowType.DownArrow)
        self.details.setVisible(shown)


class NoticeLine(QLabel):
    def __init__(self, item: NoticeItem) -> None:
        super().__init__(item.text)
        self.item = item
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setProperty("tone", "muted" if item.severity == "info" else item.severity)

    def refresh(self) -> None:
        self.setText(self.item.text)


class PermissionCard(QFrame):
    def __init__(self, request: PermissionRequest,
                 resolve: Callable[[str, Decision], None]) -> None:
        super().__init__()
        self.setObjectName("prompt")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 12, 10)
        call = request.call
        kind = "It only reads." if request.read_only else "It may change things."
        layout.addWidget(_label(f"Kayden wants to use {call.name}", role="strong"))
        layout.addWidget(_label(f"{kind} {summarize_arguments(call.arguments)}", tone="muted"))
        buttons = QHBoxLayout()
        for decision, label in ((Decision.ALLOW_ONCE, "Allow once"),
                                (Decision.ALLOW_CONVERSATION, "Allow for this conversation"),
                                (Decision.DENY, "Deny")):
            button = QPushButton(label)
            if decision is Decision.ALLOW_ONCE:
                _primary(button)
            button.clicked.connect(lambda _=False, d=decision: self._answer(request.id, d))
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.resolve = resolve
        self.setAccessibleName(f"Permission request for {call.name}")

    def _answer(self, request_id: str, decision: Decision) -> None:
        self.setEnabled(False)  # first-wins; the card goes when PermissionResolved arrives
        self.resolve(request_id, decision)


class ConversationList(QListWidget):
    """Right-click, the Menu key or Shift+F10 asks for an entry's menu."""

    menu_requested = Signal(QListWidgetItem, QPoint)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if event.reason() == QContextMenuEvent.Reason.Mouse:
            entry, at = self.itemAt(event.pos()), event.globalPos()
        else:
            entry = self.currentItem()
            at = self.viewport().mapToGlobal(
                self.visualItemRect(entry).center()) if entry else QPoint()
        if entry is not None:
            self.menu_requested.emit(entry, at)


class MessageBox(QPlainTextEdit):
    """Enter sends; Shift+Enter starts a new line. Grows with its text up to a few lines."""

    submitted = Signal()
    focus_changed = Signal(bool)
    MAX_LINES = 6

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)
        self._fit()

    def _fit(self, *_: object) -> None:
        lines = min(max(1, int(self.document().size().height())), self.MAX_LINES)
        margins = self.contentsMargins()
        height = (lines * self.fontMetrics().lineSpacing() + 2 * self.document().documentMargin()
                  + margins.top() + margins.bottom() + 2)
        self.setFixedHeight(int(height))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if enter and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event: Any) -> None:
        super().focusInEvent(event)
        self.focus_changed.emit(True)

    def focusOutEvent(self, event: Any) -> None:
        super().focusOutEvent(event)
        self.focus_changed.emit(False)


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
        self.memory_dialog: MemoryDialog | None = None
        self._conversations: dict[str, ConversationInfo] = {}
        self._models: Sequence[ModelInfo] | str | None = None  # a str says why there are none
        self.setWindowTitle("Kayden")
        self.resize(1000, 720)
        self._build()
        self._build_menus()
        self._refresh_status()

    # ── layout ────────────────────────────────────────────────────────────

    def _build(self) -> None:
        sidebar = QWidget(objectName="sidebar")
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(10, 12, 10, 10)
        side.setSpacing(8)
        self.new_action = QAction("&New chat", self, shortcut=QKeySequence.StandardKey.New)
        self.new_action.triggered.connect(lambda: self.open_conversation(None))
        self.new_button = _primary(QPushButton("New chat"))
        self.new_button.setToolTip("Start a new conversation (Ctrl+N)")
        self.new_button.clicked.connect(self.new_action.trigger)
        self.conversation_list = ConversationList()
        self.conversation_list.setItemDelegate(RowDelegate(self.conversation_list))
        self.conversation_list.setMouseTracking(True)
        self.conversation_list.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.conversation_list.setAccessibleName("Conversations")
        self.conversation_list.setToolTip("Right-click a conversation, or press the Menu key, "
                                          "to rename, archive or delete it.")
        self.conversation_list.itemActivated.connect(self._conversation_chosen)
        self.conversation_list.itemClicked.connect(self._conversation_chosen)
        self.conversation_list.menu_requested.connect(
            lambda entry, at: self.conversation_menu(self._info_of(entry)).popup(at))
        for keys, command in ((QKeySequence("F2"), self.rename_conversation),
                              (QKeySequence.StandardKey.Delete, self.delete_conversation)):
            shortcut = QShortcut(keys, self.conversation_list,
                                 context=Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(lambda c=command: self._on_current(c))
        self.show_archived = Switch("Show archived")
        self.show_archived.toggled.connect(lambda _: _spawn(self.fill_sidebar()))
        self.switch_reason = _label(selectable=False, role="small")
        side.addWidget(self.new_button)
        side.addWidget(_label("Conversations", selectable=False, role="small"))
        side.addWidget(self.conversation_list, 1)
        side.addWidget(self.show_archived)
        side.addWidget(self.switch_reason)

        main = QWidget()
        column = QVBoxLayout(main)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.banner = QFrame(objectName="banner")
        banner = QHBoxLayout(self.banner)
        banner.setContentsMargins(12, 8, 8, 8)
        self.banner_text = _label()
        self.banner_settings = QPushButton("Open Settings")
        self.banner_settings.clicked.connect(self.open_settings)
        banner.addWidget(self.banner_text, 1)
        banner.addWidget(self.banner_settings)
        self.banner.hide()

        header = QFrame(objectName="header")
        head = QHBoxLayout(header)
        head.setContentsMargins(18, 10, 14, 10)
        head.setSpacing(10)
        self.title_label = ElidedLabel()
        self.title_label.setProperty("role", "title")
        group = QFrame(objectName="headerGroup")
        controls = QHBoxLayout(group)
        controls.setContentsMargins(10, 4, 10, 4)
        controls.setSpacing(10)
        self.spending_label = _label(wrap=False, selectable=False, tone="muted")
        self.spending_label.setToolTip("What Kayden has spent so far, across all conversations.")
        controls.addWidget(self.spending_label)
        self.spending_group = group
        head.addWidget(self.title_label, 1)
        head.addWidget(group)

        self.scroll = QScrollArea(widgetResizable=True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(objectName="transcript")
        outer = QVBoxLayout(content)
        outer.setContentsMargins(20, 16, 20, 16)
        self.load_earlier = QPushButton("Load earlier messages")
        self.load_earlier.clicked.connect(self._load_earlier)
        self.load_earlier.hide()
        self.items_layout = QVBoxLayout()
        self.items_layout.setSpacing(12)
        self.empty_state = QWidget()
        empty = QVBoxLayout(self.empty_state)
        empty.addStretch(2)
        empty.addWidget(_avatar(48, "bigAvatar"), 0, Qt.AlignmentFlag.AlignHCenter)
        for line in (_label(EMPTY_TITLE, selectable=False, role="title"),
                     _label(EMPTY_HINT, selectable=False, tone="muted")):
            line.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            empty.addWidget(line)
        empty.addStretch(3)
        outer.addWidget(self.load_earlier, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addLayout(self.items_layout)
        outer.addWidget(self.empty_state, 1)
        outer.addStretch()
        self.scroll.setWidget(content)
        self._at_end = True
        bar = self.scroll.verticalScrollBar()
        bar.valueChanged.connect(lambda v: setattr(self, "_at_end", v >= bar.maximum() - 4))
        bar.rangeChanged.connect(lambda _lo, hi: self._at_end and bar.setValue(hi))

        self.prompts = QVBoxLayout()
        self.prompts.setContentsMargins(20, 0, 20, 8)
        self.composer = QFrame(objectName="composer")
        composer = QHBoxLayout(self.composer)
        composer.setContentsMargins(14, 5, 5, 5)
        composer.setSpacing(8)
        self.input = MessageBox()
        self.input.setPlaceholderText("Message Kayden")
        self.input.setToolTip("Enter sends. Shift+Enter starts a new line.")
        self.input.setAccessibleName("Message")
        self.input.submitted.connect(self._send)
        self.input.focus_changed.connect(lambda on: theme.mark(self.composer, "focused", on))
        self.send_button = QToolButton(objectName="send", text="Send")
        self.send_button.setFixedSize(32, 32)
        self.send_button.setIconSize(QSize(16, 16))
        self.send_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_button.clicked.connect(self._send_or_stop)
        composer.addWidget(self.input, 1, Qt.AlignmentFlag.AlignVCenter)
        composer.addWidget(self.send_button, 0, Qt.AlignmentFlag.AlignBottom)
        self.status_label = _label(wrap=False, selectable=False, role="small")
        self.usage_label = _label(wrap=False, selectable=False, role="small")
        footer = QHBoxLayout()
        footer.setContentsMargins(10, 4, 10, 0)
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.usage_label)
        bottom = QVBoxLayout()
        bottom.setContentsMargins(20, 0, 20, 8)
        bottom.setSpacing(0)
        bottom.addWidget(self.composer)
        bottom.addLayout(footer)

        column.addWidget(header)
        banner_row = QVBoxLayout()
        banner_row.setContentsMargins(20, 10, 20, 0)
        banner_row.addWidget(self.banner)
        column.addLayout(banner_row)
        column.addWidget(self.scroll, 1)
        column.addLayout(self.prompts)
        column.addLayout(bottom)

        splitter = QSplitter()
        splitter.setHandleWidth(1)
        splitter.addWidget(sidebar)
        splitter.addWidget(main)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([250, 750])
        self.setCentralWidget(splitter)
        self._paint_icons()

    def _paint_icons(self) -> None:
        colors = theme.current()
        self.send_button.setIcon(theme.icon("stop" if self.model.busy else "send",
                                            colors.on_accent))

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:  # the system switched light or dark
            self._paint_icons()

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&Kayden")
        settings = QAction("&Settings…", self, shortcut=QKeySequence.StandardKey.Preferences)
        settings.triggered.connect(self.open_settings)
        stop = QAction("S&top", self, shortcut=QKeySequence("Esc"))
        stop.triggered.connect(self.controller.stop)
        quit_action = QAction("&Quit", self, shortcut=QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        self.memory_action = QAction("&Memory…", self)
        self.memory_action.triggered.connect(self.open_memory)
        self.app_menu_action = QAction("&Add to App Menu", self)
        self.app_menu_action.setStatusTip("Start Kayden from your app menu, with its own icon.")
        self.app_menu_action.triggered.connect(self._add_to_app_menu)
        self.app_menu_action.setVisible(app_menu.supported() and not app_menu.is_current())
        for action in (self.new_action, None, self.memory_action,
                       settings, self.app_menu_action, None, stop, quit_action):
            if action is None:
                file_menu.addSeparator()
            else:
                file_menu.addAction(action)
        self.model_menu = self.menuBar().addMenu("&Model")
        self.model_menu.aboutToShow.connect(self._fill_model_menu)
        self._fill_model_menu()

    # ── startup ───────────────────────────────────────────────────────────

    async def begin(self, conversation_id: str | None = None) -> None:
        """When Kayden could not start: the welcome on first run, else the settings with
        the error. Otherwise open `conversation_id`, or resume the newest conversation."""
        settings = await self.runtime.settings()
        if needs_welcome(settings):
            self._dialog = WelcomeDialog(self, self.runtime, self.controller, settings)
            self._dialog.accepted.connect(lambda: self.controller.command(self.runtime.restart()))
            self._dialog.open()
            return
        if settings.startup_error is not None:
            self.open_settings()
            return
        await self.check_memory()
        if self.runtime.snapshot().conversation is None:
            if conversation_id is None:
                conversations = await self.controller.conversations()
                conversation_id = conversations[0].id if conversations else None
            self.open_conversation(conversation_id)

    async def check_memory(self) -> None:
        """Hide Memory when the Memory module cannot show what it remembers."""
        self.memory_action.setVisible(await self.controller.memories() is not None)

    def open_conversation(self, conversation_id: str | None, *,
                          seed_from: str | None = None) -> None:
        if reason := self.controller.can_switch():
            self.model.notify(reason)
            _spawn(self.fill_sidebar())  # select the open conversation again
            return
        task = self.controller.open_conversation(conversation_id, seed_from=seed_from)
        task.add_done_callback(lambda _: (self.input.setFocus(), _spawn(self.fill_sidebar())))

    # ── view model changes ────────────────────────────────────────────────

    def _changed(self, change: Change) -> None:
        match change:
            case Reset():
                self._rebuild()
                _spawn(self.fill_sidebar())
                _spawn(self.refresh_models())
            case ConversationListChanged():
                _spawn(self.fill_sidebar())
            case MemoryListChanged():
                if self.memory_dialog is not None and self.memory_dialog.isVisible():
                    self.memory_dialog.refresh()
            case Inserted(index=index, item=item):
                widget = self._widget_for(item)
                self._widgets[id(item)] = widget
                self.items_layout.insertWidget(index, widget)
                self._show_empty_state()
            case Updated(item=item):
                if (widget := self._widgets.get(id(item))) is not None:
                    widget.refresh()  # type: ignore[attr-defined]
            case Removed(item=item):
                if (widget := self._widgets.pop(id(item), None)) is not None:
                    widget.deleteLater()
                self._show_empty_state()
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

    def _show_empty_state(self) -> None:
        self.empty_state.setVisible(self.model.conversation is not None and not self.model.items
                                    and not self.model.has_earlier)

    def _refresh_status(self) -> None:
        model = self.model
        self.status_label.setText(model.status_text)
        self.usage_label.setText(model.model or "")
        conversation = model.conversation
        if conversation is not None:
            self.title_label.setText(conversation_label(
                self._conversations.get(conversation.id, conversation)))
        self.spending_label.setText(model.spending_text)
        self.spending_group.setVisible(bool(model.spending_text))
        self.send_button.setText("Stop" if model.busy else "Send")
        self.send_button.setToolTip("Stop (Esc)" if model.busy else "Send (Enter)")
        self.send_button.setAccessibleName("Stop" if model.busy else "Send message")
        self._paint_icons()
        self._show_empty_state()
        self.load_earlier.setVisible(model.has_earlier)
        self.load_earlier.setEnabled(True)
        reason = self.controller.can_switch()
        for widget in (self.new_button, self.new_action):
            widget.setEnabled(reason is None)
        self.switch_reason.setText(reason or "")
        self.switch_reason.setVisible(reason is not None)
        if (failure := model.failure) is not None:
            budget = self.controller.failure_is_budget
            self.banner_text.setText(BUDGET_MESSAGE if budget else failure.message)
            self.banner_settings.setVisible(budget)
        self.banner.setVisible(failure is not None)

    async def fill_sidebar(self) -> None:
        try:
            conversations = await self.controller.conversations(
                include_archived=self.show_archived.isChecked())
        except Exception as e:  # noqa: BLE001 - shown, never fatal
            self.model.notify(user_message(e))
            return
        current = self.model.conversation.id if self.model.conversation else None
        self._conversations = {info.id: info for info in conversations}
        self.conversation_list.clear()
        for info in conversations:
            title = conversation_label(info)
            entry = QListWidgetItem(title + ("  (archived)" if info.archived else ""))
            entry.setData(Qt.ItemDataRole.UserRole, info.id)
            entry.setData(TITLE_ROLE, title)
            entry.setData(PREVIEW_ROLE, conversation_preview(info))
            entry.setData(DATE_ROLE, relative_date(info.updated_at))
            entry.setData(FLAGS_ROLE, {"archived"} if info.archived else set())
            self.conversation_list.addItem(entry)
            if info.id == current:
                self.conversation_list.setCurrentItem(entry)
        self._refresh_status()

    def conversation_info(self, conversation_id: str) -> ConversationInfo:
        return self._conversations[conversation_id]

    def _info_of(self, entry: QListWidgetItem) -> ConversationInfo:
        return self._conversations[entry.data(Qt.ItemDataRole.UserRole)]

    def _on_current(self, command: Callable[[ConversationInfo], None]) -> None:
        if (entry := self.conversation_list.currentItem()) is not None:
            command(self._info_of(entry))

    def _conversation_chosen(self, entry: QListWidgetItem) -> None:
        conversation_id = entry.data(Qt.ItemDataRole.UserRole)
        if self.model.conversation is None or conversation_id != self.model.conversation.id:
            self.open_conversation(conversation_id)

    def conversation_menu(self, info: ConversationInfo) -> QMenu:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        menu.addAction("Rename…", lambda: self.rename_conversation(info))
        go_on = menu.addAction("Continue in a new conversation",
                               lambda: self.open_conversation(None, seed_from=info.id))
        go_on.setToolTip("Start a new conversation that knows where this one left off.")
        menu.addAction("Unarchive" if info.archived else "Archive",
                       lambda: self.controller.archive(info.id, not info.archived))
        menu.addSeparator()
        menu.addAction("Delete…", lambda: self.delete_conversation(info)).setVisible(
            self.controller.can_delete)
        return menu

    def rename_conversation(self, info: ConversationInfo) -> None:
        dialog = QInputDialog(self)
        dialog.setWindowTitle("Rename conversation")
        dialog.setLabelText("New name (leave it empty to let Kayden name it):")
        dialog.setTextValue(info.title or "")
        dialog.textValueSelected.connect(lambda title: self.controller.rename(info.id, title))
        dialog.open()

    def delete_conversation(self, info: ConversationInfo) -> None:
        box = QMessageBox(QMessageBox.Icon.NoIcon, "Delete conversation",
                          f"Delete “{conversation_label(info)}”?", parent=self)
        box.setInformativeText(delete_details(self.controller.is_open(info.id)))
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        delete = box.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
        delete.setProperty("danger", True)
        delete.style().polish(delete)
        box.setDefaultButton(cancel)
        box.buttonClicked.connect(lambda button: button is delete
                                  and self.controller.delete(info.id))
        box.open()

    def _add_to_app_menu(self) -> None:
        try:
            app_menu.install()
        except OSError as e:
            self.model.notify(f"Kayden could not be added to the app menu: {e}")
            return
        self.app_menu_action.setVisible(False)
        self.model.notify("Kayden is now in your app menu.")

    def open_memory(self) -> None:
        if self.memory_dialog is None:
            self.memory_dialog = MemoryDialog(self, self.controller)
        self.memory_dialog.refresh()
        self.memory_dialog.open()

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

    async def refresh_models(self) -> None:
        try:
            self._models = await self.runtime.list_models()
        except Exception as e:  # noqa: BLE001 - e.g. no key yet
            self._models = user_message(e)
        self._fill_model_menu()

    def _fill_model_menu(self) -> None:
        """From the models already loaded: a menu that is empty when clicked does not
        open, so it is never filled while it opens."""
        menu = self.model_menu
        menu.clear()
        if self._models is None or isinstance(self._models, str):
            menu.addAction(self._models or "Loading models…").setEnabled(False)
            return
        for info in self._models:
            action = menu.addAction(model_label(info))
            action.setData(info.id)
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


# ── long-term memory ──────────────────────────────────────────────────────


class MemoryDialog(QDialog):
    """What Kayden remembers across conversations; the user adds, edits and forgets."""

    def __init__(self, parent: QWidget, controller: Controller) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Memory")
        self.setMinimumSize(520, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.addWidget(_label("Memory", selectable=False, role="heading"))
        layout.addWidget(_label("Kayden remembers these things across conversations.",
                                tone="muted"))
        self.entries = QListWidget()
        self.entries.setAccessibleName("Memories")
        self.entries.setItemDelegate(RowDelegate(self.entries, wrap=True))
        self.entries.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.entries.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.entries.setMouseTracking(True)
        self.entries.currentItemChanged.connect(lambda *_: self._selection_changed())
        self.entries.itemActivated.connect(lambda _: self._edit())
        self.empty = _label("Nothing yet. Kayden adds things here as it learns them, and you "
                            "can add your own.", selectable=False, tone="muted")
        layout.addWidget(self.entries, 1)
        layout.addWidget(self.empty)
        self.add_button = _primary(QPushButton("Add…"))
        self.add_button.clicked.connect(self._add)
        self.edit_button = QPushButton("Edit…")
        self.edit_button.clicked.connect(self._edit)
        self.forget_button = QPushButton("Forget…")
        self.forget_button.clicked.connect(self._forget)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        for button in (self.add_button, self.edit_button, self.forget_button):
            row.addWidget(button)
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)
        self._selection_changed()

    def refresh(self) -> None:
        async def load() -> None:
            entries = await self.controller.memories()
            selected = self._selected()
            self.entries.clear()
            for entry in entries or ():
                item = QListWidgetItem(entry.text)
                item.setData(Qt.ItemDataRole.UserRole, entry)
                item.setData(PREVIEW_ROLE, memory_source(entry))
                item.setToolTip(memory_source(entry))
                self.entries.addItem(item)
                if selected is not None and entry.id == selected.id:
                    self.entries.setCurrentItem(item)
            self.empty.setVisible(not entries)
            self._selection_changed()

        self.controller.command(load())

    def _selected(self) -> MemoryEntry | None:
        item = self.entries.currentItem()
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _selection_changed(self) -> None:
        for button in (self.edit_button, self.forget_button):
            button.setEnabled(self._selected() is not None)

    def _ask(self, title: str, text: str, then: Callable[[str], None]) -> None:
        dialog = QInputDialog(self)
        dialog.setWindowTitle(title)
        dialog.setLabelText("What should Kayden remember?")
        dialog.setOption(QInputDialog.InputDialogOption.UsePlainTextEditForTextInput)
        dialog.setTextValue(text)
        dialog.textValueSelected.connect(then)
        dialog.open()

    def _add(self) -> None:
        self._ask("Add to memory", "", self.controller.add_memory)

    def _edit(self) -> None:
        if (entry := self._selected()) is not None:
            self._ask("Edit memory", entry.text,
                      lambda text: self.controller.edit_memory(entry.id, text))

    def _forget(self) -> None:
        if (entry := self._selected()) is not None:
            confirm(self, f"Forget “{clip(entry.text, 80)}”?",
                    lambda: self.controller.forget_memory(entry.id), yes="Forget", danger=True,
                    details="Kayden won't use it again. The messages it came from stay until "
                            "you delete their conversation.")


# ── settings ──────────────────────────────────────────────────────────────


def confirm(parent: QWidget, text: str, then: Callable[[], None], *, yes: str = "Continue",
            details: str = "", danger: bool = False) -> QMessageBox:
    """Cancel is the default; `yes` names the action."""
    box = QMessageBox(QMessageBox.Icon.NoIcon, "Kayden", text, parent=parent)
    box.setInformativeText(details)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    action = box.addButton(yes, QMessageBox.ButtonRole.AcceptRole)
    if danger:
        action.setProperty("danger", True)
        action.style().polish(action)
    box.setDefaultButton(cancel)
    box.buttonClicked.connect(lambda button: button is action and then())
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
    """Sends only what changed; loosening and a different Memory are confirmed first.
    Module settings wait for a restart, which Restart now starts."""

    def __init__(self, parent: QWidget, runtime: RuntimeAPI, controller: Controller,
                 settings: Settings, models: Sequence[ModelInfo], spent: float) -> None:
        super().__init__(parent)
        self.runtime, self.controller, self.settings = runtime, controller, settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        form.setContentsMargins(20, 18, 20, 16)
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(14)
        form.addRow(_label("Settings", selectable=False, role="heading"))

        self.startup_error = _label(f"Kayden could not start: {settings.startup_error}")
        theme.mark(self.startup_error, "tone", "error")
        self.startup_error.setVisible(settings.startup_error is not None)
        self.modules = {kind: self._module_box(kind) for kind in settings.modules}
        self.modules["core"].currentIndexChanged.connect(self._core_changed)
        self.modules["frontend"].currentIndexChanged.connect(
            lambda: self.restart_button.setVisible(True))
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
        self.has_models = bool(models)
        for box in (self.model, self.light):
            box.setEnabled(self.has_models)
        self.model_note = _label("After the restart, the new provider starts with its first "
                                 "model.", selectable=False, role="small")
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
        self.notice = _label(restart_notice(settings) or "", selectable=False, tone="warning")
        self.notice.setVisible(restart_notice(settings) is not None)
        self.error = _label()
        theme.mark(self.error, "tone", "error")
        self.error.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.save_button = _primary(buttons.button(QDialogButtonBox.StandardButton.Save))
        self.restart_button = buttons.addButton("Restart now",
                                                QDialogButtonBox.ButtonRole.ActionRole)
        self.restart_button.clicked.connect(lambda: self._save(restart=True))
        self.restart_button.setVisible(settings.restart_needed
                                       or settings.startup_error is not None)

        form.addRow(self.startup_error)
        form.addRow("AI provider", self.modules["core"])
        form.addRow("API key", self.api_key)
        form.addRow("Model", self.model)
        form.addRow("Model for memory upkeep", self.light)
        form.addRow("", self.model_note)
        form.addRow("Spending limit (US$)", self.limit)
        form.addRow("", _label(f"{dollars(spent)} spent so far", selectable=False, role="small"))
        form.addRow("What the agent may do", self.permissions)
        form.addRow("Instructions", self.instructions)
        form.addRow("Work folder", folder_row)
        for kind in ("memory", "tools", "frontend"):
            form.addRow(MODULE_LABELS[kind], self.modules[kind])
        form.addRow(_label(AT_RESTART, selectable=False, role="small"))
        form.addRow(self.notice)
        form.addRow(self.error)
        form.addRow(buttons)

    def _module_box(self, kind: str) -> QComboBox:
        choice = self.settings.modules[kind]
        box = QComboBox()
        for label, module_id in module_options(choice):
            box.addItem(label, module_id)
        if choice.chosen is None:
            box.insertItem(0, "Custom (from config.toml)", None)
        box.setCurrentIndex(max(0, box.findData(choice.chosen)))
        return box

    def _core_changed(self) -> None:
        switching = self.modules["core"].currentData() != self.settings.modules["core"].running
        self.model.setEnabled(not switching and self.has_models)
        self.light.setEnabled(not switching and self.has_models)
        self.model_note.setVisible(switching)

    def form(self) -> SettingsForm:
        return SettingsForm(
            modules={kind: box.currentData() for kind, box in self.modules.items()},
            api_key=self.api_key.text(),
            model=self.model.currentData(),
            maintenance_model=self.light.currentData(),
            spending_limit=self.limit.text(),
            permissions=PermissionPolicy(self.permissions.currentData()),
            instructions=self.instructions.toPlainText(),
            work_folder=self.folder.text() if self.folder.isEnabled() else None,
        )

    def _save(self, *, restart: bool = False) -> None:
        try:
            update = settings_update(self.settings, self.form())
        except ValueError as e:
            self._show_error(str(e))
            return
        if update == SettingsUpdate() and not restart:
            self.accept()
            return
        text = ""
        if reasons := loosening(self.settings, update):
            text = "This change " + " and ".join(reasons) + ". "
        if update.modules and "memory" in update.modules:
            text += MEMORY_WARNING + " "
        if text:
            confirm(self, "Save these changes?", lambda: self._apply(update, restart),
                    yes="Save", details=text.strip())
        else:
            self._apply(update, restart)

    def _apply(self, update: SettingsUpdate, restart: bool) -> None:
        self.save_button.setEnabled(False)
        self.error.hide()

        async def apply() -> Settings:
            settings = self.settings
            if update != SettingsUpdate():
                settings = await self.runtime.update_settings(update)
            if restart:
                await self.runtime.restart()
            return settings

        def failed(error: Exception) -> None:
            self.save_button.setEnabled(True)
            self._show_error(user_message(error))

        def saved(settings: Settings) -> None:
            self.accept()
            if not restart and needs_restart(update) and (notice := restart_notice(settings)):
                confirm(self.parentWidget(), "Restart Kayden now?",
                        lambda: self.controller.command(self.runtime.restart()),
                        yes="Restart now", details=notice)

        self.controller.command(apply(), done=saved, failed=failed)

    def _show_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()


# ── first run ─────────────────────────────────────────────────────────────


class WelcomeDialog(QDialog):
    """Three pages: provider, key, then work folder and spending limit, saved on the setup
    runtime before anything is built. Accepted when everything is saved; the window then
    restarts Kayden, which builds the provider with its key."""

    def __init__(self, parent: QWidget, runtime: RuntimeAPI, controller: Controller,
                 settings: Settings) -> None:
        super().__init__(parent)
        self.runtime, self.controller, self.settings = runtime, controller, settings
        self.setWindowTitle("Welcome to Kayden")
        self.setMinimumSize(520, 400)
        self.pages = QStackedWidget()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.addWidget(_label("Welcome to Kayden", selectable=False, role="heading"))
        layout.addWidget(self.pages)

        # 1. provider
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 8, 0, 0)
        column.addWidget(_label("Choose your AI provider. Kayden uses its models and you pay "
                                "it directly for what you use."))
        self.provider = QListWidget(objectName="choices")
        self.provider.setAccessibleName("AI provider")
        self.cores = [m for m in settings.modules["core"].installed if m.key_name is not None]
        for core in self.cores:
            entry = QListWidgetItem(core.name)
            entry.setData(Qt.ItemDataRole.UserRole, core.id)
            self.provider.addItem(entry)
        chosen = settings.modules["core"].chosen
        self.provider.setCurrentRow(max(0, next(
            (i for i, core in enumerate(self.cores) if core.id == chosen), 0)))
        column.addWidget(self.provider)
        next_button = _primary(QPushButton("Continue"))
        next_button.setDefault(True)
        next_button.clicked.connect(lambda: self.pages.setCurrentIndex(1))
        column.addLayout(_right(next_button))
        self.pages.addWidget(page)

        # 2. key
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 8, 0, 0)
        self.key_intro = _label()
        get_key = QPushButton("Get a key")
        get_key.clicked.connect(self._open_key_page)
        self.key = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Paste your API key")
        self.key.setAccessibleName("API key")
        self.key_error = _label()
        theme.mark(self.key_error, "tone", "error")
        self.key_error.hide()
        column.addWidget(self.key_intro)
        column.addWidget(get_key, 0, Qt.AlignmentFlag.AlignLeft)
        column.addWidget(self.key)
        column.addWidget(self.key_error)
        column.addStretch()
        self.key_continue = _primary(QPushButton("Save the key"))
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
        column.setContentsMargins(0, 8, 0, 0)
        column.addWidget(_label("Kayden runs commands and works with files in this folder."))
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
        theme.mark(self.finish_error, "tone", "error")
        self.finish_error.hide()
        column.addWidget(self.finish_error)
        column.addStretch()
        self.finish_button = _primary(QPushButton("Start using Kayden"))
        self.finish_button.clicked.connect(self._finish)
        column.addLayout(_right(self.finish_button))
        self.pages.addWidget(page)

    def _chosen(self) -> Any:
        provider_id = self.provider.currentItem().data(Qt.ItemDataRole.UserRole)
        return next(core for core in self.cores if core.id == provider_id)

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
        chosen = self.settings.modules["core"].chosen
        update = SettingsUpdate(api_key=key, modules=None if provider.id == chosen
                                else {"core": provider.id})
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

