"""Qt widgets for the desktop app. They render the view model and turn clicks into
Controller commands; no widget awaits the runtime.

Dialogs use open() and signals, never exec(): exec() would run a nested event loop inside
the qasync loop that also runs the runtime.
"""

from __future__ import annotations

import asyncio
import getpass
import html
import json
from collections.abc import Callable, Container, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QContextMenuEvent,
    QCursor,
    QDesktopServices,
    QEnterEvent,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
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
    Duty,
    McpServer,
    MemoryNote,
    ModelInfo,
    PermissionPolicy,
    PermissionRequest,
    PlanItem,
    Proposal,
    RuntimeAPI,
    Settings,
    SettingsUpdate,
    ToolStatus,
)
from kayden.frontends.common import (
    AT_RESTART,
    DUTY_HINT,
    DUTY_KIND_LABELS,
    MCP_VARIABLES_HINT,
    MCP_WARNING,
    MEMORY_HINT,
    MODULE_LABELS,
    PERMISSION_LABELS,
    PERMISSION_SHORT,
    PLAN_HINT,
    Card,
    call_parts,
    delete_details,
    dollars,
    duty_status,
    folder_path,
    loosening,
    mcp_server,
    mcp_target,
    mcp_variables,
    model_label,
    module_options,
    needs_restart,
    parse_plan_text,
    plan_text,
    proposal_parts,
    request_note,
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
    group_heading,
    limit_update,
    memory_source,
    needs_welcome,
    relative_age,
    relative_date,
    settings_update,
    step_segments,
    user_message,
)
from kayden.frontends.remote import RemoteRuntime
from kayden.frontends.sessions import SubSession, SubSessions

LINE_HEIGHT_PERCENT = 118  # reply text, of the font's line spacing (itself ~1.35 em)
CODE_SCALE = 0.88  # inline code, of the text size: monospace looks larger at the same size
PARAGRAPH_SPACING = 10  # px below a reply paragraph
TURN_GAP = 18  # px more below your message, before Kayden's reply, than between its steps
LIST_ITEM_SPACING = 4  # px below a list item
ZOOM_SAVE_DELAY_S = 0.4
HEADING_SPACING = 6  # px above a heading, on top of the paragraph below the previous block
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
COLUMN_WIDTH = 780  # px: the conversation and the message box, centred
RECENT_FOLDERS = 6  # a new chat's folder menu: its own and recent ones
COPIED_MS = 1500  # a reply's copy button shows a tick this long
MASCOT_WIDTH = 200  # px: the new chat's mascot
ASSETS = Path(__file__).parent / "assets"
SIDEBAR_WIDTH = 320  # px at 100%: room for a title and a sub-session's under it
TOOL_TONES = {ToolStatus.OK: "ok", ToolStatus.ERROR: "error", ToolStatus.ABANDONED: "warning",
              ToolStatus.TIMED_OUT: "warning", ToolStatus.INTERRUPTED: "warning"}
# Item data the sidebar and memory rows are drawn from.
TITLE_ROLE = Qt.ItemDataRole.UserRole + 1
PREVIEW_ROLE = Qt.ItemDataRole.UserRole + 2
DATE_ROLE = Qt.ItemDataRole.UserRole + 3
FLAGS_ROLE = Qt.ItemDataRole.UserRole + 4  # {"archived"}, {"folded"}, {"working"}
KIND_ROLE = Qt.ItemDataRole.UserRole + 5  # sidebar rows: "group", "conversation", "session"


def _spawn(coro: Any) -> asyncio.Task[Any]:
    return asyncio.ensure_future(coro)


async def load_zoom(path: Path) -> float:
    """The zoom saved in `path`; 1.0 when there is none or it is unreadable."""
    def read() -> float:
        try:
            return float(json.loads(path.read_text(encoding="utf-8"))["zoom"])
        except (OSError, ValueError, TypeError, KeyError):
            return 1.0
    return await asyncio.to_thread(read)


async def save_zoom(path: Path, level: float) -> None:
    await asyncio.to_thread(path.write_text, json.dumps({"zoom": level}), encoding="utf-8")


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


def _mascot(width: int) -> QLabel:
    """Kayden's mascot, peeking: sharp on high-density screens."""
    label = QLabel(objectName="mascot")
    ratio = label.devicePixelRatioF() or 1.0
    pixmap = QPixmap(str(ASSETS / "mascot-peek.png")).scaledToWidth(
        round(width * ratio), Qt.TransformationMode.SmoothTransformation)
    pixmap.setDevicePixelRatio(ratio)
    label.setPixmap(pixmap)
    label.setAccessibleName("Kayden")
    return label


def _discard(widget: QWidget) -> None:
    """Hidden at once: deleteLater runs only when the event loop next turns, and until then
    a widget taken out of its layout would still be drawn where it was. Its timers stop, so a
    pending redraw cannot show it again."""
    for timer in widget.findChildren(QTimer):
        timer.stop()
    widget.hide()
    widget.deleteLater()


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
    With `wrap`, the title wraps instead of ending in "…". In the sidebar (KIND_ROLE) a
    project heading is a small foldable label, and a sub-session is indented under its
    conversation with a tree line."""

    PAD = QSize(10, 7)
    INDENT = 22  # a sub-session under its conversation

    def __init__(self, view: QListWidget, *, wrap: bool = False, compact: bool = False
                 ) -> None:
        super().__init__(view)
        self.view, self.wrap, self.compact = view, wrap, compact
        if compact:
            self.PAD = QSize(10, 6)

    def _texts(self, index: Any) -> tuple[str, str, str, set[str]]:
        title = index.data(TITLE_ROLE) or index.data(Qt.ItemDataRole.DisplayRole) or ""
        flags = index.data(FLAGS_ROLE) or set()
        if index.data(KIND_ROLE) is not None:  # the sidebar: one line, details on hover
            return title, "", "", flags
        return (title, index.data(PREVIEW_ROLE) or "", index.data(DATE_ROLE) or "", flags)

    def _title_font(self, option: QStyleOptionViewItem) -> QFont:
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold if not self.wrap and not self.compact
                       else QFont.Weight.Normal)
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
        if index.data(KIND_ROLE) == "group":
            height = QFontMetrics(self._small_font(option)).height()
            return QSize(self.view.viewport().width(), height + 2 * self.PAD.height() + 4)
        title, preview, _, _ = self._texts(index)
        width = self.view.viewport().width() - 2 * self.PAD.width()
        if index.data(KIND_ROLE) == "session":
            width -= self.INDENT
        height = self._title_height(option, title, width)
        if preview:
            height += 2 + QFontMetrics(self._small_font(option)).height()
        return QSize(width, height + 2 * self.PAD.height() + 2)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Any) -> None:
        kind = index.data(KIND_ROLE)
        if kind == "group":
            self._paint_group(painter, option, index)
            return
        if kind == "session":
            option = self._paint_branch(painter, option, index)
        self._paint_row(painter, option, index)

    def _paint_group(self, painter: QPainter, option: QStyleOptionViewItem, index: Any) -> None:
        """A project heading: its name, then a chevron that says whether it is folded."""
        colors = theme.current()
        folded = "folded" in (index.data(FLAGS_ROLE) or set())
        font = self._small_font(option)
        font.setWeight(QFont.Weight.DemiBold)
        name = index.data(Qt.ItemDataRole.DisplayRole)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(font)
        painter.setPen(QColor(colors.muted))
        metrics = QFontMetrics(font)
        top = option.rect.top() + self.PAD.height() + 2
        left = option.rect.left() + self.PAD.width()
        painter.drawText(left, top, metrics.horizontalAdvance(name) + 1, metrics.height(),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        x = left + metrics.horizontalAdvance(name) + 7
        y = top + metrics.height() / 2
        r = metrics.height() * 0.16
        painter.setPen(QPen(QColor(colors.muted), 1.3, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        chevron = QPainterPath()
        if folded:  # pointing right
            chevron.moveTo(x, y - r * 1.4)
            chevron.lineTo(x + r * 1.2, y)
            chevron.lineTo(x, y + r * 1.4)
        else:  # pointing down
            chevron.moveTo(x - r * 0.2, y - r * 0.6)
            chevron.lineTo(x + r * 1.2, y + r * 0.8)
            chevron.lineTo(x + r * 2.6, y - r * 0.6)
        painter.drawPath(chevron)
        painter.restore()

    def _paint_branch(self, painter: QPainter, option: QStyleOptionViewItem, index: Any
                      ) -> QStyleOptionViewItem:
        """A sub-session's guide line, one thin line down its conversation's sub-sessions,
        and a dot while it works; the row's option, moved right of them."""
        colors = theme.current()
        rect = option.rect
        x = rect.left() + self.INDENT // 2
        below = index.model().index(index.row() + 1, 0)
        last = not below.isValid() or below.data(KIND_ROLE) != "session"
        painter.save()
        painter.setPen(QPen(QColor(colors.line), 1))
        painter.drawLine(x, rect.top(), x, rect.center().y() + 6 if last else rect.bottom())
        working = "working" in (index.data(FLAGS_ROLE) or set())
        if working:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colors.accent))
            painter.drawEllipse(QRectF(rect.right() - 16, rect.top() + self.PAD.height() + 5,
                                       6, 6))
        painter.restore()
        moved = QStyleOptionViewItem(option)
        moved.rect = rect.adjusted(self.INDENT, 0, -12 if working else 0, 0)  # clear of the dot
        return moved

    def _paint_row(self, painter: QPainter, option: QStyleOptionViewItem, index: Any) -> None:
        colors = theme.current()
        title, preview, date, flags = self._texts(index)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(option.rect).adjusted(0, 1, 0, -1)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colors.select if self.compact else
                                    colors.accent_soft if selected else colors.card))
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
        painter.setPen(QColor(colors.accent_text if selected and not self.compact else
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

    def setMarkdown(self, markdown: str) -> None:
        """Markdown with room to breathe: taller lines, space between paragraphs and list
        items, more above headings; inline code a little smaller, on a soft background."""
        super().setMarkdown(markdown)
        code = QTextCharFormat()
        code.setFontPointSize(self.font().pointSizeF() * CODE_SCALE)
        inline = QTextCharFormat(code)
        inline.setBackground(QColor(theme.current().select))
        block = self.document().begin()
        while block.isValid():
            fragments = block.begin()
            spans = []
            while not fragments.atEnd():
                fragment = fragments.fragment()
                if fragment.charFormat().fontFixedPitch():
                    spans.append((fragment.position(), fragment.length()))
                fragments += 1
            for start, length in spans:
                cursor = QTextCursor(self.document())
                cursor.setPosition(start)
                cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(code if block.blockFormat().nonBreakableLines()
                                       else inline)
            fmt = block.blockFormat()
            fmt.setLineHeight(LINE_HEIGHT_PERCENT,
                              QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            if block.textList() is not None:
                fmt.setTopMargin(0)
                fmt.setBottomMargin(LIST_ITEM_SPACING)
            else:
                fmt.setBottomMargin(PARAGRAPH_SPACING)
            if fmt.headingLevel():
                fmt.setTopMargin(HEADING_SPACING)
            QTextCursor(block).setBlockFormat(fmt)
            block = block.next()

    def _fit(self, *_: object) -> None:
        self.document().setTextWidth(self.viewport().width())
        self.setFixedHeight(int(self.document().size().height()) + 4)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._fit()


class UserBubble(QWidget):
    """Your message: an accent bubble on the right."""

    def __init__(self, item: UserItem) -> None:
        super().__init__()
        self.item = item
        row = QHBoxLayout(self)
        row.setContentsMargins(0, TURN_GAP // 3, 0, TURN_GAP)  # a turn starts here
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
        margins = self.bubble.layout().contentsMargins()  # the padding around the text
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
        # Shown while the pointer is on the reply: copy it, and how long ago it came.
        self.footer = QWidget()
        footer = QHBoxLayout(self.footer)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        self.copy = QToolButton(objectName="replyAction")
        self.copy.setToolTip("Copy")
        self.copy.setAccessibleName("Copy reply")
        self.copy.setIconSize(QSize(15, 15))
        self.copy.clicked.connect(self._copy)
        self.age = _label(selectable=False, role="small")
        footer.addWidget(self.copy)
        footer.addWidget(self.age)
        footer.addStretch()
        policy = self.footer.sizePolicy()
        policy.setRetainSizeWhenHidden(True)  # no jump when it shows
        self.footer.setSizePolicy(policy)
        self.footer.hide()
        self._copied = QTimer(self, singleShot=True, interval=COPIED_MS)
        self._copied.timeout.connect(self._paint_copy)
        self._paint_copy()
        layout.addWidget(self.thinking, 0, Qt.AlignmentFlag.AlignLeft)
        for widget in (self.reasoning, self.body, self.marker, self.footer):
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

    def enterEvent(self, event: QEnterEvent) -> None:
        super().enterEvent(event)
        if self.item.final and self.item.text:
            self.age.setText(relative_age(self.item.created_at)
                             if self.item.created_at is not None else "")
            self.footer.show()

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        self.footer.hide()

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.item.text)
        self._paint_copy(copied=True)
        self._copied.start()

    def _paint_copy(self, copied: bool = False) -> None:
        self.copy.setIcon(theme.icon("check" if copied else "copy", theme.current().muted))
        self.copy.setToolTip("Copied" if copied else "Copy")

    def _toggle_thinking(self, shown: bool) -> None:
        self.thinking.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)
        self.reasoning.setVisible(shown)

    def _render(self) -> None:
        item = self.item
        self.thinking.setVisible(bool(item.reasoning))
        self.thinking.setText("Thinking…" if not item.final and not item.text else "Thinking")
        self.reasoning.setText(item.reasoning)
        self.body.setVisible(bool(item.text))
        self.body.setMarkdown(item.text)
        marker = ("The model declined to answer." if item.refused
                  else "Interrupted before it finished." if item.interrupted else "")
        self.marker.setText(marker)
        self.marker.setVisible(bool(marker))
        # A turn can carry thinking with no readable text and only call tools.
        self.setVisible(bool(item.text or item.reasoning or marker))


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
        self.details = QPlainTextEdit(readOnly=True)
        self.details.setMaximumHeight(200)
        self.details.hide()
        for widget in (self.progress, self.details):
            layout.addWidget(widget)
        self.refresh()

    def refresh(self) -> None:
        item = self.item
        self.name.setText(item.name)
        self.name.setVisible(item.sentence is None)  # a sentence says what the tool is
        duration = f" in {item.duration_s:.1f} s" if item.duration_s is not None else ""
        self.title.setText(item.summary)
        self.title.setToolTip(f"{item.name}: {item.detail}"
                              + (f" ({item.state_label}{duration})" if item.status else ""))
        theme.mark(self.title, "tone", None if item.sentence else "muted")
        self.state.setText(item.state_label + duration)
        self.state.setVisible(item.status is not ToolStatus.OK)
        theme.mark(self.state, "tone", TOOL_TONES.get(item.status, "muted")
                   if item.status is not None else "muted")
        self.cancel.setVisible(item.cancellable)
        self.progress.setText(item.progress)
        self.progress.setVisible(bool(item.progress) and item.status is None)
        self.details.setPlainText(f"{item.arguments}\n\n{item.content}".strip())
        self.setAccessibleName(f"Tool {item.name}, {item.state_label}")

    def _toggle(self, shown: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.UpArrow if shown else Qt.ArrowType.DownArrow)
        self.details.setVisible(shown)


class StepGroup(QFrame):
    """A run of steps, and the thinking between them, folded into one line: the step under
    way while it runs, then how many there were and what went wrong. Opens on a click."""

    def __init__(self, cancel: Callable[[str], None]) -> None:
        super().__init__()
        self.setObjectName("steps")
        self.active: ToolItem | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.header = QFrame(objectName="stepsHeader")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.mousePressEvent = lambda event: self.toggle.click()  # type: ignore[method-assign]
        head = QHBoxLayout(self.header)
        head.setContentsMargins(0, 2, 6, 2)
        head.setSpacing(6)
        self.toggle = QToolButton(checkable=True)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setAccessibleName("Show the steps")
        self.toggle.toggled.connect(self._toggle)
        self.title = ElidedLabel()
        self.title.setProperty("tone", "muted")
        self.state = _label(wrap=False, selectable=False)
        self.cancel = QPushButton("Cancel")
        self.cancel.setAccessibleName("Cancel this task")
        self.cancel.clicked.connect(lambda: self.active and cancel(self.active.call_id))
        head.addWidget(self.toggle)
        head.addWidget(self.title, 1)
        head.addWidget(self.state)
        head.addWidget(self.cancel)
        layout.addWidget(self.header)
        self.body = QFrame(objectName="stepsBody")
        self.steps = QVBoxLayout(self.body)
        self.steps.setContentsMargins(12, 0, 0, 0)
        self.steps.setSpacing(2)
        self.body.hide()
        layout.addWidget(self.body)

    def set_members(self, widgets: Sequence[QWidget]) -> None:
        for index, widget in enumerate(widgets):
            _place(self.steps, index, widget)
        self.refresh()

    def members(self) -> list[QWidget]:
        return [w for i in range(self.steps.count()) if (w := self.steps.itemAt(i).widget())]

    def refresh(self) -> None:
        steps = [w.item for w in self.members() if isinstance(w, ToolCard)]
        title, state, self.active = group_heading(steps)
        self.title.setText(title)
        self.title.setToolTip("\n".join(step.summary for step in steps))
        self.state.setText(state)
        tone = "muted" if self.active is not None else "error" if state else None
        theme.mark(self.state, "tone", tone)
        self.state.setVisible(bool(state))
        self.cancel.setVisible(self.active is not None and self.active.cancellable)

    def _toggle(self, shown: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)
        self.toggle.setAccessibleName("Hide the steps" if shown else "Show the steps")
        self.body.setVisible(shown)


def _place(layout: QVBoxLayout, index: int, widget: QWidget) -> None:
    """Put `widget` at `index` of `layout`, taking it out of wherever it was."""
    if layout.indexOf(widget) == index:
        return
    hidden = widget.isHidden() and widget.testAttribute(
        Qt.WidgetAttribute.WA_WState_ExplicitShowHide)
    _detach(widget)
    layout.insertWidget(index, widget)
    widget.setHidden(hidden)  # a new parent hides it, and Qt would show it only later


def _detach(widget: QWidget) -> None:
    """Take `widget` out of its layout, which may be nested in its parent's."""
    parent = widget.parentWidget()
    layouts = [parent.layout()] if parent is not None and parent.layout() is not None else []
    while layouts:
        layout = layouts.pop()
        if layout.indexOf(widget) >= 0:
            layout.removeWidget(widget)
            return
        layouts.extend(child for i in range(layout.count())
                       if (child := layout.itemAt(i).layout()) is not None)


NOTICE_LINKS = {"memory": "Open memory"}


def is_long(text: str) -> bool:
    return "\n" in text or len(text) > 160


MODULE_HINTS = {"tools": "What Kayden can do on this computer.",
                "frontend": "How Kayden looks: this window or the terminal."}


def _setting(title: str, description: str, control: QWidget | QHBoxLayout, *,
             below: bool = False) -> QWidget:
    """One row of Settings: its name and what it does on the left, the control on the
    right (or under them, for a big one)."""
    row = QFrame(objectName="settingRow")
    layout = QVBoxLayout(row) if below else QHBoxLayout(row)
    layout.setContentsMargins(0, 12, 0, 12)
    layout.setSpacing(16 if not below else 8)
    text = QVBoxLayout()
    text.setSpacing(2)
    text.addWidget(_label(title, selectable=False))
    text.addWidget(_label(description, selectable=False, role="small"))
    layout.addLayout(text, 1)
    if isinstance(control, QHBoxLayout):
        holder = QWidget()
        holder.setLayout(control)
        control = holder
    if not below:
        control.setFixedWidth(300)
    layout.addWidget(control)
    return row


def _page(title: str, *rows: QWidget) -> QScrollArea:
    page = QWidget(objectName="settingsPage")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(4, 16, 18, 4)
    layout.setSpacing(0)
    layout.addWidget(_label(title, selectable=False, role="title"))
    layout.addSpacing(6)
    for row in rows:
        layout.addWidget(row)
    layout.addStretch()
    scroll = QScrollArea(widgetResizable=True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(page)
    return scroll


class DragArea(QFrame):
    """Where the window is grabbed: dragging moves it, a double-click maximizes or restores
    it, as a title bar would. Buttons and fields inside it work as usual."""

    def mousePressEvent(self, event: QMouseEvent) -> None:
        handle = self.window().windowHandle()
        if event.button() == Qt.MouseButton.LeftButton and handle is not None:
            handle.startSystemMove()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()


class WindowEdges(QObject):
    """Resizing a window without a frame: near an edge the cursor shows the direction, and
    pressing there hands the resize to the window system."""

    MARGIN = 6  # px from the edge

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.window = window
        self.cursor_set = False
        QApplication.instance().installEventFilter(self)

    def edges(self, global_pos: QPoint) -> Qt.Edge:
        if self.window.isMaximized() or self.window.isFullScreen():
            return Qt.Edge(0)
        pos = self.window.mapFromGlobal(global_pos)
        rect, m = self.window.rect(), self.MARGIN
        found = Qt.Edge(0)
        if pos.x() <= m:
            found |= Qt.Edge.LeftEdge
        elif pos.x() >= rect.width() - m:
            found |= Qt.Edge.RightEdge
        if pos.y() <= m:
            found |= Qt.Edge.TopEdge
        elif pos.y() >= rect.height() - m:
            found |= Qt.Edge.BottomEdge
        return found

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind not in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress):
            return False
        if not isinstance(watched, QWidget) or watched.window() is not self.window:
            return False
        edges = self.edges(event.globalPosition().toPoint())  # type: ignore[attr-defined]
        if kind == QEvent.Type.MouseMove:
            self._show_cursor(edges)
            return False
        handle = self.window.windowHandle()
        if edges and handle is not None and event.button() == Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
            handle.startSystemResize(edges)
            return True
        return False

    def _show_cursor(self, edges: Qt.Edge) -> None:
        shape = _EDGE_CURSORS.get(edges)
        if shape is None and self.cursor_set:
            QApplication.restoreOverrideCursor()
            self.cursor_set = False
        elif shape is not None:
            if self.cursor_set:
                QApplication.changeOverrideCursor(QCursor(shape))
            else:
                QApplication.setOverrideCursor(QCursor(shape))
                self.cursor_set = True


_E = Qt.Edge
_EDGE_CURSORS = {
    _E.LeftEdge: Qt.CursorShape.SizeHorCursor, _E.RightEdge: Qt.CursorShape.SizeHorCursor,
    _E.TopEdge: Qt.CursorShape.SizeVerCursor, _E.BottomEdge: Qt.CursorShape.SizeVerCursor,
    _E.TopEdge | _E.LeftEdge: Qt.CursorShape.SizeFDiagCursor,
    _E.BottomEdge | _E.RightEdge: Qt.CursorShape.SizeFDiagCursor,
    _E.TopEdge | _E.RightEdge: Qt.CursorShape.SizeBDiagCursor,
    _E.BottomEdge | _E.LeftEdge: Qt.CursorShape.SizeBDiagCursor,
}


class NoticeLine(QLabel):
    """A line from Kayden itself; with a link, linkActivated carries the item's link."""

    def __init__(self, item: NoticeItem) -> None:
        super().__init__()
        self.item = item
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setProperty("tone", "muted" if item.severity == "info" else item.severity)
        self.refresh()

    def refresh(self) -> None:
        if self.item.link is None:
            self.setTextFormat(Qt.TextFormat.PlainText)
            self.setText(self.item.text)
            return
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setText(f"{html.escape(self.item.text)} · "
                     f'<a href="{self.item.link}">{NOTICE_LINKS[self.item.link]}</a>')


class Detail(QWidget):
    """Text that may run long, folded to its start with Show all: a call's arguments on a
    card (a sub-session's brief, a long command), or a long notice (a new goal)."""

    def __init__(self, text: str, *, lines: int = 3, chars: int = 240) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        short = "\n".join(text.splitlines()[:lines])
        if len(short) > chars:
            short = short[:chars].rstrip()
        self.full, self.short = text, (short + "…" if short != text else text)
        self.label = _label(self.short, tone="muted")
        layout.addWidget(self.label)
        self.toggle = QToolButton(text="Show all")
        self.toggle.setVisible(self.short != text)
        self.toggle.clicked.connect(self._toggled)
        layout.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignLeft)

    def _toggled(self) -> None:
        expanded = self.label.text() == self.full
        self.label.setText(self.short if expanded else self.full)
        self.toggle.setText("Show all" if expanded else "Show less")

    def refresh(self) -> None:
        pass  # its text does not change


class PermissionCard(QFrame):
    def __init__(self, request: PermissionRequest,
                 resolve: Callable[[str, Decision], None]) -> None:
        super().__init__()
        self.setObjectName("prompt")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 12, 10)
        call = request.call
        headline, detail = call_parts(call.arguments)
        layout.addWidget(_label(f"Kayden wants to: {headline}" if headline
                                else f"Kayden wants to use {call.name}", role="strong"))
        layout.addWidget(_label(request_note(request), tone="muted"))
        if detail:
            layout.addWidget(Detail(detail))
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


class ProposalCard(QFrame):
    """A call the agent proposed: it runs only if the user approves it."""

    def __init__(self, proposal: Proposal, resolve: Callable[[str, bool], None]) -> None:
        super().__init__()
        self.setObjectName("prompt")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 12, 10)
        headline, detail = proposal_parts(proposal)
        layout.addWidget(_label(headline, role="strong"))
        layout.addWidget(_label(proposal.reason, tone="muted"))
        if detail:
            layout.addWidget(Detail(detail))
        buttons = QHBoxLayout()
        approve = _primary(QPushButton("Approve"))
        dismiss = QPushButton("Dismiss")
        approve.clicked.connect(lambda: self._answer(proposal.id, True))
        dismiss.clicked.connect(lambda: self._answer(proposal.id, False))
        buttons.addWidget(approve)
        buttons.addWidget(dismiss)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.resolve = resolve
        self.setAccessibleName(f"Proposal: {headline}")

    def _answer(self, proposal_id: str, accept: bool) -> None:
        self.setEnabled(False)  # the card goes when ProposalResolved arrives
        self.resolve(proposal_id, accept)


class Spinner(QWidget):
    """A turning arc: the step under way."""

    SIZE, STEP_MS, STEP_DEGREES, ARC_DEGREES = 16, 60, 30, 100

    def __init__(self, color: str) -> None:
        super().__init__()
        self._color, self._angle = QColor(color), 0
        self.setFixedSize(self.SIZE, self.SIZE)
        timer = QTimer(self)
        timer.timeout.connect(self._turn)
        timer.start(self.STEP_MS)

    def _turn(self) -> None:
        self._angle = (self._angle + self.STEP_DEGREES) % 360
        self.update()

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self._color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        inset = self.SIZE * 0.1
        painter.drawArc(QRectF(inset, inset, self.SIZE - 2 * inset, self.SIZE - 2 * inset),
                        (90 - self._angle) * 16, self.ARC_DEGREES * 16)


class PlanCard(QFrame):
    """The agent's plan as a checklist: done steps ticked and dimmed, the one under way with
    a turning arc, the rest open; dropped ones struck through."""

    def __init__(self, plan: Sequence[PlanItem]) -> None:
        super().__init__()
        self.setObjectName("plan")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        colors = theme.current()
        self._rows: list[tuple[str, str]] = []
        for item in plan:
            mark = str(item.status)  # done, doing, todo or dropped
            row = QHBoxLayout()
            row.setSpacing(8)
            icon: QWidget
            if mark == "doing":
                icon = Spinner(colors.muted)
            else:
                icon = QLabel()
                icon.setPixmap(theme.icon(mark, colors.muted, 16).pixmap(16, 16))
            text = _label(item.text, tone=None if mark in ("doing", "todo") else "muted")
            if mark == "dropped":
                font = text.font()
                font.setStrikeOut(True)
                text.setFont(font)
            # Beside the first line of the step's text, also when the text wraps.
            icon.setFixedSize(16, 16)
            lead = max(0, (text.fontMetrics().height() - 16) // 2)
            holder = QVBoxLayout()
            holder.setContentsMargins(0, lead, 0, 0)
            holder.addWidget(icon)
            holder.addStretch()
            row.addLayout(holder)
            row.addWidget(text, 1)
            layout.addLayout(row)
            self._rows.append((mark, item.text))
        self.setAccessibleName("Plan")

    def rows(self) -> list[tuple[str, str]]:
        """(mark, text) per step, for tests and accessibility checks."""
        return list(self._rows)


class WrapUpCard(QFrame):
    """The card that ends the agent's turn: its recap, its question and the options, its pick
    first. A click sends an option as the user's message, or runs its call while that is
    pending; the close button puts the card away."""

    def __init__(self, card: Card, pending: Container[str], choose: Callable[[int], None],
                 close: Callable[[], None]) -> None:
        super().__init__()
        self.setObjectName("prompt")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 8, 10)
        top = QHBoxLayout()
        text = QVBoxLayout()
        if card.recap:
            text.addWidget(_label(card.recap, tone="muted"))
        if card.question:
            text.addWidget(_label(card.question, role="strong"))
        top.addLayout(text, 1)
        dismiss = QToolButton(objectName="windowControl")
        dismiss.setIcon(theme.icon("close", theme.current().muted))
        dismiss.setToolTip("Close")
        dismiss.setAccessibleName("Close")
        dismiss.clicked.connect(close)
        top.addWidget(dismiss, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)
        if card.options:
            buttons = QHBoxLayout()
            for i, option in enumerate(card.options):
                button = QPushButton(option)
                if i == 0:
                    _primary(button)
                button.setToolTip("Do this" if card.runs(i, pending) else "Send this reply")
                button.clicked.connect(lambda _=False, index=i: choose(index))
                buttons.addWidget(button)
            buttons.addStretch()
            layout.addLayout(buttons)
        self.setAccessibleName(card.question or "Turn summary")


@dataclass(frozen=True, slots=True)
class Opening:
    """What the user last asked to see, while it opens: a sub-session, else a conversation
    (None: a new chat). The newest request wins; the sidebar highlights it meanwhile."""

    request: int
    conversation: str | None = None
    session: SubSession | None = None


class ConversationList(QListWidget):
    """A click or Enter chooses an entry: `chosen`, once per click. (itemActivated is not
    used: where a single click activates, as on KDE, it comes with itemClicked.) Right-click,
    the Menu key or Shift+F10 asks for an entry's menu."""

    chosen = Signal(QListWidgetItem)
    menu_requested = Signal(QListWidgetItem, QPoint)

    def __init__(self) -> None:
        super().__init__()
        self.itemClicked.connect(self.chosen)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
                entry := self.currentItem()) is not None:
            self.chosen.emit(entry)
            return
        super().keyPressEvent(event)

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
    MIN_LINES = 2  # room to write, as other chat apps leave
    MAX_LINES = 6

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)
        self._fit()

    def _fit(self, *_: object) -> None:
        lines = min(max(self.MIN_LINES, int(self.document().size().height())), self.MAX_LINES)
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

    def __init__(self, runtime: RuntimeAPI, controller: Controller,
                 zoom_file: Path | None = None, sessions: SubSessions | None = None) -> None:
        super().__init__()
        self.runtime = runtime
        self.controller = controller
        self.sessions = sessions  # the sub-sessions this window hosts
        self.home = controller  # this window's own runtime; `controller` is the one shown
        controller.sessions = sessions
        self._viewing: tuple[SubSession, RemoteRuntime, asyncio.Task[None]] | None = None
        self._collapsed: set[str] = set()  # sidebar groups the user folded
        self.zoom_file = zoom_file  # where the zoom is remembered; None: not remembered
        self._zoom_saver: asyncio.Task[None] | None = None
        self.model: ConversationModel = controller.model
        self.model.on_change = self._changed
        self._widgets: dict[int, QWidget] = {}  # by id() of its item
        self._groups: dict[int, StepGroup] = {}  # by id() of its first step
        self._arrange_pending = False
        self._dialog: QDialog | None = None
        self.memory_dialog: MemoryDialog | None = None
        self.mcp_dialog: McpDialog | None = None
        self._conversations: dict[str, ConversationInfo] = {}
        self._opening: Opening | None = None
        self._requests = 0
        self._models: Sequence[ModelInfo] | str | None = None  # a str says why there are none
        self.setWindowTitle("Kayden")
        # No title bar from the desktop: the header and the sidebar's top row move the
        # window, the header has its controls, and the edges resize it (WindowEdges).
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self._edges = WindowEdges(self)
        self.resize(1000, 720)
        self._build()
        self._build_menus()
        self._refresh_status()
        if sessions is not None:
            sessions.listeners.append(self._sessions_changed)

    # ── layout ────────────────────────────────────────────────────────────

    def _build(self) -> None:
        sidebar = QWidget(objectName="sidebar")
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(8, 8, 8, 10)
        side.setSpacing(2)
        self.new_action = QAction("&New chat", self, shortcut=QKeySequence.StandardKey.New)
        self.new_action.triggered.connect(lambda: self.open_conversation(None))
        self.menu_button = QToolButton(objectName="menu")
        self.menu_button.setIconSize(QSize(18, 18))
        self.menu_button.setToolTip("Menu")
        self.menu_button.setAccessibleName("Menu")
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.new_button = QPushButton("  New chat", objectName="nav")
        self.new_button.setIconSize(QSize(16, 16))
        self.new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_button.setToolTip("Start a new conversation (Ctrl+N)")
        self.new_button.clicked.connect(self.new_action.trigger)
        self.conversation_list = ConversationList()
        self.conversation_list.setObjectName("conversations")
        self.conversation_list.setItemDelegate(RowDelegate(self.conversation_list, compact=True))
        self.conversation_list.setMouseTracking(True)
        self.conversation_list.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.conversation_list.setAccessibleName("Conversations")
        self.conversation_list.setToolTip("Right-click a conversation, or press the Menu key, "
                                          "to rename, archive or delete it.")
        self.conversation_list.chosen.connect(self._conversation_chosen)
        self.conversation_list.menu_requested.connect(self._row_menu)
        for keys, command in ((QKeySequence("F2"), self.rename_conversation),
                              (QKeySequence.StandardKey.Delete, self.delete_conversation)):
            shortcut = QShortcut(keys, self.conversation_list,
                                 context=Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(lambda c=command: self._on_current(c))
        self.show_archived = QAction("Show &archived conversations", self, checkable=True)
        self.show_archived.toggled.connect(lambda _: _spawn(self.fill_sidebar()))
        self.switch_reason = _label(selectable=False, role="small")
        self.user_name = getpass.getuser()
        self.profile_button = QToolButton(objectName="profile", text=f" {self.user_name}")
        self.profile_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.profile_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.profile_button.setIconSize(QSize(22, 22))
        self.profile_button.setAccessibleName("Profile and settings")
        self.profile_button.setSizePolicy(QSizePolicy.Policy.Expanding,
                                          QSizePolicy.Policy.Fixed)
        self.gear_button = QToolButton(objectName="gear")
        self.gear_button.setToolTip("Settings (Ctrl+,)")
        self.gear_button.setAccessibleName("Settings")
        self.gear_button.setIconSize(QSize(18, 18))
        top_row = DragArea()
        top = QHBoxLayout(top_row)
        top.setContentsMargins(0, 0, 0, 6)
        top.addWidget(self.menu_button)
        top.addStretch()
        side.addWidget(top_row)
        side.addWidget(self.new_button)
        side.addSpacing(10)
        side.addWidget(self.conversation_list, 1)
        side.addWidget(self.switch_reason)
        profile_row = QHBoxLayout()
        profile_row.setContentsMargins(0, 6, 0, 0)
        profile_row.addWidget(self.profile_button, 1)
        profile_row.addWidget(self.gear_button)
        side.addLayout(profile_row)

        main = QWidget()
        column = QVBoxLayout(main)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.banner = QFrame(objectName="banner")
        banner = QHBoxLayout(self.banner)
        banner.setContentsMargins(12, 8, 8, 8)
        self.banner_text = _label()
        self.banner_settings = QPushButton("Open Settings")
        self.banner_settings.clicked.connect(lambda: self.open_settings())
        banner.addWidget(self.banner_text, 1)
        banner.addWidget(self.banner_settings)
        self.banner.hide()

        header = DragArea(objectName="header")
        head = QHBoxLayout(header)
        head.setContentsMargins(18, 0, 0, 0)  # the window buttons reach the top and right
        head.setSpacing(4)
        self.title_label = ElidedLabel()
        self.title_label.setProperty("role", "header")
        self.duty_label = _label(wrap=False, selectable=False, role="small")
        self.duty_label.hide()  # only with a duty: when it checks in next, or paused
        titles = QHBoxLayout()
        titles.setContentsMargins(0, 14, 0, 14)  # the header's height; the buttons fill it
        titles.setSpacing(4)
        titles.addWidget(self.title_label, 1)
        titles.addWidget(self.duty_label)
        head.addLayout(titles, 1)
        head.addSpacing(8)
        self.window_buttons: dict[str, QToolButton] = {}
        for name, tip, command in (("minimize", "Minimize", self.showMinimized),
                                   ("maximize", "Maximize", self._toggle_maximized),
                                   ("close", "Close", self.close)):
            button = QToolButton(objectName="titleClose" if name == "close"
                                 else "titleControl")
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            button.setIconSize(QSize(16, 16))
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
            button.clicked.connect(command)
            head.addWidget(button)
            self.window_buttons[name] = button

        self.scroll = QScrollArea(widgetResizable=True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(objectName="transcript")
        centred = QHBoxLayout(content)
        centred.setContentsMargins(24, 16, 24, 24)
        reading = QWidget()
        reading.setMaximumWidth(COLUMN_WIDTH)
        centred.addWidget(reading)
        outer = QVBoxLayout(reading)
        outer.setContentsMargins(0, 0, 0, 0)
        self.load_earlier = QPushButton("Load earlier messages")
        self.load_earlier.clicked.connect(self._load_earlier)
        self.load_earlier.hide()
        self.items_layout = QVBoxLayout()
        self.items_layout.setSpacing(12)
        self.empty_state = QWidget()
        empty = QVBoxLayout(self.empty_state)
        empty.addStretch(2)
        empty.addWidget(_mascot(MASCOT_WIDTH), 0, Qt.AlignmentFlag.AlignHCenter)
        for line in (_label(EMPTY_TITLE, selectable=False, role="title"),
                     _label(EMPTY_HINT, selectable=False, tone="muted")):
            line.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            empty.addWidget(line)
        # A new chat's folder, and recent ones to start it in instead; only before the first
        # message (the header's tooltip shows it after).
        self.folder_button = QToolButton(objectName="folderPicker")
        self.folder_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.folder_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.folder_button.setIconSize(QSize(18, 18))
        self.folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.folder_button.setAccessibleName("Work folder")
        folder_menu = QMenu(self, objectName="folderMenu")
        folder_menu.aboutToShow.connect(self._fill_folder_menu)
        self.folder_button.setMenu(folder_menu)
        self.folder_button.hide()
        empty.addSpacing(16)
        empty.addWidget(self.folder_button, 0, Qt.AlignmentFlag.AlignHCenter)
        empty.addStretch(3)
        outer.addWidget(self.load_earlier, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addLayout(self.items_layout)
        self.waiting_line = _label("Thinking…", selectable=False, tone="muted")
        self.waiting_line.hide()
        outer.addWidget(self.waiting_line)
        outer.addWidget(self.empty_state, 1)
        outer.addStretch()
        self.scroll.setWidget(content)
        self._at_end = True
        bar = self.scroll.verticalScrollBar()
        bar.valueChanged.connect(lambda v: setattr(self, "_at_end", v >= bar.maximum() - 4))
        bar.rangeChanged.connect(lambda _lo, hi: self._at_end and bar.setValue(hi))

        self.prompts = QVBoxLayout()
        self.prompts.setContentsMargins(0, 0, 0, 8)
        self.composer = QFrame(objectName="composer")
        composer = QVBoxLayout(self.composer)
        composer.setContentsMargins(16, 12, 10, 8)
        composer.setSpacing(4)
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
        composer.addWidget(self.input)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(self.send_button)
        composer.addLayout(actions)
        self.model_button = QToolButton(objectName="modelPicker")
        self.model_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.model_button.setToolTip("Change the model")
        self.model_button.setAccessibleName("Model")
        footer = QHBoxLayout()
        footer.setContentsMargins(10, 4, 4, 0)
        self.permissions_button = QToolButton(objectName="modelPicker")
        self.permissions_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.permissions_button.setAccessibleName("What the agent may do")
        permissions_menu = QMenu(self)
        self.permission_actions: dict[PermissionPolicy, QAction] = {}
        for policy, label in PERMISSION_LABELS.items():
            action = permissions_menu.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(lambda _=False, p=policy: self.choose_permissions(p))
            self.permission_actions[policy] = action
        self.permissions_button.setMenu(permissions_menu)
        footer.addStretch(1)
        footer.addWidget(self.permissions_button)
        footer.addWidget(self.model_button)
        bottom_column = QWidget()
        bottom_column.setMaximumWidth(COLUMN_WIDTH)
        bottom = QVBoxLayout(bottom_column)
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(0)
        bottom.addLayout(self.prompts)
        bottom.addWidget(self.composer)
        bottom.addLayout(footer)
        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(24, 0, 24, 10)
        bottom_row.addWidget(bottom_column)

        column.addWidget(header)
        banner_row = QVBoxLayout()
        banner_row.setContentsMargins(20, 10, 20, 0)
        banner_row.addWidget(self.banner)
        column.addLayout(banner_row)
        column.addWidget(self.scroll, 1)
        column.addLayout(bottom_row)

        splitter = QSplitter()
        splitter.setHandleWidth(1)
        splitter.addWidget(sidebar)
        splitter.addWidget(main)
        splitter.setStretchFactor(1, 1)
        sidebar_width = round(SIDEBAR_WIDTH * theme.zoom())
        splitter.setSizes([sidebar_width, max(self.width() - sidebar_width, COLUMN_WIDTH)])
        self.setCentralWidget(splitter)
        self._paint_icons()

    def _paint_icons(self) -> None:
        colors = theme.current()
        self.send_button.setIcon(theme.icon("stop" if self.model.busy else "send",
                                            colors.text))
        self.gear_button.setIcon(theme.icon("gear", colors.muted, 18))
        self.menu_button.setIcon(theme.icon("menu", colors.muted, 18))
        self.new_button.setIcon(theme.icon("plus", colors.text, 16))
        self.folder_button.setIcon(theme.icon("folder", colors.text, 18))
        maximized = self.isMaximized()
        for name, button in self.window_buttons.items():
            kind = "restore" if name == "maximize" and maximized else name
            button.setIcon(theme.icon(kind, colors.muted))
        self.window_buttons["maximize"].setToolTip("Restore" if maximized else "Maximize")
        self.profile_button.setIcon(theme.avatar(self.user_name))

    def _choose_folder(self) -> None:
        if reason := self.controller.can_switch():
            self.model.notify(reason)
            return
        folder = self.model.conversation.folder if self.model.conversation else None
        choose_folder(self, folder or str(Path.home()), self.folder_chosen)

    def _fill_folder_menu(self) -> None:
        """The open chat's folder, then those of recent conversations, newest first."""
        menu = self.folder_button.menu()
        menu.clear()
        own = self.model.conversation.folder if self.model.conversation else None
        recent = [info.folder for info in sorted(self._conversations.values(),
                                                 key=lambda i: i.updated_at, reverse=True)
                  if info.folder is not None]
        for folder in list(dict.fromkeys([own, *recent]))[:RECENT_FOLDERS]:
            if folder is None:
                continue
            action = menu.addAction(Path(folder).name or folder)
            action.setCheckable(True)
            action.setChecked(folder == own)
            action.setToolTip(folder_path(folder))
            action.triggered.connect(lambda _=False, f=folder: self.folder_chosen(f))
        menu.setToolTipsVisible(True)
        menu.addSeparator()
        menu.addAction("Choose folder…", self._choose_folder)

    def folder_chosen(self, folder: str) -> None:
        """Start a chat in `folder`; an open chat with no messages moves there."""
        if not folder or (self.model.conversation is not None
                          and folder == self.model.conversation.folder):
            return
        task = self.home.new_in_folder(folder)
        task.add_done_callback(lambda _: (self.input.setFocus(), _spawn(self.fill_sidebar())))

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange,  # the system switched light or dark
                            QEvent.Type.WindowStateChange):  # maximize becomes restore
            self._paint_icons()

    def choose_permissions(self, policy: PermissionPolicy) -> None:
        """Change what the agent may do; doing more without asking is confirmed first."""
        current = self.home.model.permissions
        self._refresh_status()  # the menu shows the current choice until it changes

        def apply() -> None:
            self.home.command(self.runtime.update_settings(SettingsUpdate(permissions=policy)))

        order = list(PermissionPolicy)
        if current is not None and order.index(policy) > order.index(current):
            confirm(self, "Let the agent do more without asking?", apply, yes="Allow",
                    details=PERMISSION_LABELS[policy] + ".")
        elif policy != current:
            apply()

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _build_menus(self) -> None:
        """No menu bar. The ☰ menu holds what is about the conversation (new chat, duty,
        stop, view, model); the profile menu at the bottom holds the app (settings, memory,
        tool servers, help, quit). Every action is also the window's, so its shortcut works
        with the menus closed."""
        self.menuBar().hide()
        settings = QAction("&Settings…", self)
        settings.setShortcuts([QKeySequence("Ctrl+,")])  # Preferences is unbound on Linux
        settings.triggered.connect(lambda: self.open_settings())
        stop = QAction("S&top", self, shortcut=QKeySequence("Esc"))
        stop.triggered.connect(self.controller.stop)
        quit_action = QAction("&Quit", self, shortcut=QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        self.memory_action = QAction("&Memory…", self)
        self.memory_action.triggered.connect(self.open_memory)
        self.duty_action = QAction("&Duty…", self)
        self.duty_action.setStatusTip("Give this conversation standing work it carries on "
                                      "by itself.")
        self.duty_action.triggered.connect(self.open_duty)
        servers = QAction("&Tool servers (MCP)…", self)
        servers.triggered.connect(self.open_tool_servers)
        self.app_menu_action = QAction("&Add to App Menu", self)
        self.app_menu_action.setStatusTip("Start Kayden from your app menu, with its own icon.")
        self.app_menu_action.triggered.connect(self._add_to_app_menu)
        self.app_menu_action.setVisible(app_menu.supported() and not app_menu.is_current())
        help_action = QAction("&Help", self, shortcut=QKeySequence.StandardKey.HelpContents)
        help_action.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(DOCS_URL)))

        self.folder_action = QAction("New chat in another &folder…", self)
        self.folder_action.triggered.connect(self._choose_folder)
        main_menu = QMenu(self)
        self.menu_button.setMenu(main_menu)
        main_menu.addActions([self.new_action, self.folder_action, self.duty_action, stop])
        main_menu.addSeparator()
        view_menu = main_menu.addMenu("&View")
        for text, keys, steps in (
                ("Zoom &In", [QKeySequence.StandardKey.ZoomIn, QKeySequence("Ctrl+=")], 1),
                ("Zoom &Out", [QKeySequence.StandardKey.ZoomOut], -1),
                ("&Actual Size", [QKeySequence("Ctrl+0")], 0)):
            action = QAction(text, self)
            action.setShortcuts(keys)
            action.triggered.connect(lambda _=False, s=steps: self.zoom(s))
            view_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(self.show_archived)
        self.model_menu = main_menu.addMenu("&Model")
        self.model_menu.aboutToShow.connect(self._fill_model_menu)
        self._fill_model_menu()
        self.model_button.setMenu(self.model_menu)

        profile_menu = QMenu(self)
        self.profile_button.setMenu(profile_menu)
        self.profile_header = profile_menu.addAction(self.user_name)
        self.profile_header.setEnabled(False)
        profile_menu.aboutToShow.connect(self._fill_profile_header)
        profile_menu.addSeparator()
        profile_menu.addActions([settings, self.memory_action, servers])
        profile_menu.addSeparator()
        profile_menu.addActions([self.app_menu_action, help_action])
        profile_menu.addSeparator()
        profile_menu.addAction(quit_action)
        self.gear_button.clicked.connect(settings.trigger)
        for menu in (main_menu, view_menu, profile_menu):
            self.addActions([a for a in menu.actions() if not a.menu()])

    def _fill_profile_header(self) -> None:
        spent = self.model.spending_text
        self.profile_header.setText(f"{self.user_name} · {spent}" if spent else self.user_name)

    def zoom(self, steps: int) -> None:
        """Make everything larger (steps > 0), smaller, or its normal size (0)."""
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        theme.set_zoom(app, theme.zoom_step(theme.zoom(), steps) if steps else 1.0)
        if self.zoom_file is None:
            return
        if self._zoom_saver is not None:  # once the keys stop: one write, never a stale one
            self._zoom_saver.cancel()
        self._zoom_saver = _spawn(self._save_zoom(self.zoom_file))

    async def _save_zoom(self, path: Path) -> None:
        await asyncio.sleep(ZOOM_SAVE_DELAY_S)
        await save_zoom(path, theme.zoom())

    async def save_pending_zoom(self) -> None:
        """Write a zoom change still waiting for its delay, before the window goes."""
        if self._zoom_saver is not None and not self._zoom_saver.done():
            self._zoom_saver.cancel()
            assert self.zoom_file is not None
            await save_zoom(self.zoom_file, theme.zoom())

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
                conversations = await self.home.conversations()
                conversation_id = conversations[0].id if conversations else None
            self.open_conversation(conversation_id)

    async def check_memory(self) -> None:
        """Hide Memory when the Memory module cannot show what it remembers."""
        self.memory_action.setVisible(await self.controller.notes() is not None)

    def open_conversation(self, conversation_id: str | None, *,
                          seed_from: str | None = None) -> None:
        # Marked first: leaving a sub-session redraws the list, which then shows this one.
        opening = self._open(conversation=conversation_id)
        if self._viewing is not None:
            self._leave_session()
            own = self.model.conversation
            if own is not None and conversation_id == own.id and seed_from is None:
                self._settle(opening)
                return
        if reason := self.controller.can_switch():
            self._settle(opening)
            self.model.notify(reason)
            return
        task = self.controller.open_conversation(conversation_id, seed_from=seed_from)
        task.add_done_callback(lambda _: self._opened(opening))

    def _open(self, *, conversation: str | None = None,
              session: SubSession | None = None) -> Opening:
        self._requests += 1
        self._opening = Opening(self._requests, conversation, session)
        return self._opening

    def _settle(self, opening: Opening) -> bool:
        """`opening` is done; True when it was the newest request, so what it opened shows."""
        if self._opening is not opening:
            return False
        self._opening = None
        self._render_sidebar()
        return True

    def _opened(self, opening: Opening) -> None:
        if self._settle(opening):
            self.input.setFocus()
            _spawn(self.fill_sidebar())
            self._deliver_notes()

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
            case Inserted(item=item):
                self._widgets[id(item)] = self._widget_for(item)
                self._arrange_soon()
                self._show_empty_state()
            case Updated(item=item):
                if (widget := self._widgets.get(id(item))) is not None:
                    widget.refresh()  # type: ignore[attr-defined]
                    if isinstance(group := self._group_of(widget), StepGroup):
                        if isinstance(item, AssistantItem) and (item.text or item.refused):
                            self._arrange_soon()  # a reply now: out of the group
                        else:
                            group.refresh()
            case Removed(item=item):
                if (widget := self._widgets.pop(id(item), None)) is not None:
                    _detach(widget)
                    _discard(widget)
                self._arrange_soon()
                self._show_empty_state()
            case PromptsChanged():
                self._rebuild_prompts()
            case StatusChanged():
                self._refresh_status()
            case Closed():
                if self.controller is self.home:
                    self.close()

    def _widget_for(self, item: Item) -> QWidget:
        match item:
            case UserItem():
                return UserBubble(item)
            case AssistantItem():
                return AssistantBubble(item)
            case ToolItem():
                return ToolCard(item, self.controller.cancel)
            case NoticeItem() if item.link is None and is_long(item.text):
                return Detail(item.text, lines=1, chars=140)  # a new goal's brief
            case NoticeItem():
                line = NoticeLine(item)
                line.linkActivated.connect(self._notice_link)
                return line

    def _rebuild(self) -> None:
        for widget in (*self._widgets.values(), *self._groups.values()):
            _discard(widget)
        self._widgets.clear()
        self._groups.clear()
        for item in self.model.items:
            self._widgets[id(item)] = self._widget_for(item)
        self._arrange()
        self._at_end = True
        self._rebuild_prompts()
        self._refresh_status()

    def _arrange_soon(self) -> None:
        """Arrange once the current burst of changes is in (a page of history, a commit
        that replaces what streamed), not once per item."""
        if not self._arrange_pending:
            self._arrange_pending = True
            asyncio.get_running_loop().call_soon(self._arrange)

    def _arrange(self) -> None:
        """Lay the item widgets out as step_segments groups them, keeping each group (and
        whether it is open) for as long as its first step stays first."""
        self._arrange_pending = False
        groups: dict[int, StepGroup] = {}
        for index, segment in enumerate(step_segments(self.model.items)):
            if isinstance(segment, list):
                key = id(next(item for item in segment if isinstance(item, ToolItem)))
                group = self._groups.get(key) or StepGroup(self.controller.cancel)
                groups[key] = group
                group.set_members([self._widgets[id(item)] for item in segment])
                _place(self.items_layout, index, group)
            else:
                _place(self.items_layout, index, self._widgets[id(segment)])
        for key, group in self._groups.items():
            if key not in groups:
                _detach(group)
                _discard(group)
        self._groups = groups

    @staticmethod
    def _group_of(widget: QWidget) -> QWidget | None:
        body = widget.parentWidget()
        return body.parentWidget() if body is not None else None

    def _rebuild_prompts(self) -> None:
        while (child := self.prompts.takeAt(0)) is not None:
            if child.widget() is not None:
                _discard(child.widget())
        if steps := self.model.checklist:
            self.prompts.addWidget(PlanCard(steps))
        for request in self.model.prompts.values():
            self.prompts.addWidget(PermissionCard(request, self.controller.resolve))
        for proposal in self.model.proposals.values():
            if not proposal.card:
                self.prompts.addWidget(ProposalCard(proposal, self.controller.resolve_proposal))
        if (card := self.model.card) is not None:
            self.prompts.addWidget(WrapUpCard(card, self.model.proposals, self._choose,
                                              self.model.close_card))

    def _choose(self, index: int) -> None:
        if (card := self.model.card) is None:
            return
        if card.runs(index, self.model.proposals):
            self.controller.resolve_proposal(card.call_id, True)
            self.model.close_card()
        else:
            self.controller.send(card.options[index])
        self.input.setFocus()

    def _show_empty_state(self) -> None:
        self.empty_state.setVisible(self.model.conversation is not None and not self.model.items
                                    and not self.model.has_earlier)

    def _refresh_status(self) -> None:
        model = self.model
        self.waiting_line.setVisible(model.waiting)
        duty = (duty_status(model.conversation.duty, model.duty_wake_at, model.state,
                            model.help_needed) if model.conversation is not None else None)
        self.duty_label.setText(duty or "")
        self.duty_label.setVisible(duty is not None)
        known = () if self._models is None or isinstance(self._models, str) else self._models
        names = {m.id: m.name for m in known}
        self.model_button.setText(names.get(model.model or "", model.model or ""))
        policy = self.home.model.permissions
        self.permissions_button.setVisible(policy is not None)
        if policy is not None:
            self.permissions_button.setText(PERMISSION_SHORT[policy])
            self.permissions_button.setToolTip(f"What the agent may do: "
                                               f"{PERMISSION_LABELS[policy].lower()}")
            for each, action in self.permission_actions.items():
                action.setChecked(each == policy)
        conversation = model.conversation
        if conversation is not None:
            self.title_label.setText(conversation_label(
                self._conversations.get(conversation.id, conversation)))
        folder = conversation.folder if conversation is not None else None
        self.title_label.setToolTip(f"Kayden works in {folder_path(folder)}"
                                    if folder is not None else "")
        if hasattr(self, "folder_action"):  # built after the first status
            self.folder_action.setVisible(folder is not None)
        new_chat = (self._viewing is None and folder is not None and not model.items
                    and not model.has_earlier)
        self.folder_button.setVisible(new_chat)
        if folder is not None:
            self.folder_button.setText(f"  {Path(folder).name or folder}  ⌄")
            self.folder_button.setToolTip(f"Kayden works in {folder_path(folder)}")
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
            conversations = await self.home.conversations(
                include_archived=self.show_archived.isChecked())
        except Exception as e:  # noqa: BLE001 - shown, never fatal
            self.model.notify(user_message(e))
            return
        self._conversations = {info.id: info for info in conversations}
        self._render_sidebar()

    def _render_sidebar(self) -> None:
        """Conversations grouped by project (their folder), newest first, each with its
        sub-sessions under it. A folded group shows only its heading."""
        # What the user last asked for stays highlighted while it opens, not what was open.
        opening, own = self._opening, self.home.model.conversation
        if opening is not None:
            current, shown = opening.conversation, opening.session
        else:
            current = own.id if own is not None else None
            shown = self._viewing[0] if self._viewing is not None else None
        viewing = shown.id if shown is not None else None
        archived = self.show_archived.isChecked()
        groups: dict[str | None, list[ConversationInfo]] = {}
        for info in self._conversations.values():
            groups.setdefault(info.folder, []).append(info)
        headed = len(groups) > 1 or None not in groups
        self.conversation_list.clear()
        for folder, members in groups.items():
            key = folder or ""
            if headed:
                heading = QListWidgetItem(Path(folder).name if folder else "Other")
                heading.setData(KIND_ROLE, "group")
                heading.setData(Qt.ItemDataRole.UserRole, key)
                heading.setData(FLAGS_ROLE, {"folded"} if key in self._collapsed else set())
                heading.setToolTip(folder_path(folder) if folder else "")
                heading.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.conversation_list.addItem(heading)
                if key in self._collapsed:
                    continue
            for info in members:
                title = conversation_label(info)
                entry = QListWidgetItem(title + ("  (archived)" if info.archived else ""))
                entry.setData(KIND_ROLE, "conversation")
                entry.setData(Qt.ItemDataRole.UserRole, info.id)
                entry.setData(TITLE_ROLE, title)
                entry.setData(PREVIEW_ROLE, conversation_preview(info))
                entry.setData(DATE_ROLE, relative_date(info.updated_at))
                entry.setData(FLAGS_ROLE, {"archived"} if info.archived else set())
                entry.setToolTip(conversation_label(info, full=True))
                self.conversation_list.addItem(entry)
                if viewing is None and info.id == current:
                    self.conversation_list.setCurrentItem(entry)
                children = (self.sessions.of(info.id, include_archived=archived)
                            if self.sessions else ())
                for session in children:
                    row = QListWidgetItem(session.name)
                    row.setData(KIND_ROLE, "session")
                    row.setData(Qt.ItemDataRole.UserRole, session.id)
                    row.setData(TITLE_ROLE, session.name)
                    row.setData(PREVIEW_ROLE, session.summary)
                    row.setData(FLAGS_ROLE, {"working"} if session.running else
                                {"archived"} if session.archived else set())
                    row.setToolTip(session.name)
                    self.conversation_list.addItem(row)
                    if session.id == viewing:
                        self.conversation_list.setCurrentItem(row)
        self._refresh_status()

    def conversation_info(self, conversation_id: str) -> ConversationInfo:
        return self._conversations[conversation_id]

    def _info_of(self, entry: QListWidgetItem) -> ConversationInfo | None:
        if entry.data(KIND_ROLE) not in (None, "conversation"):
            return None
        return self._conversations[entry.data(Qt.ItemDataRole.UserRole)]

    def _on_current(self, command: Callable[[ConversationInfo], None]) -> None:
        entry = self.conversation_list.currentItem()
        if entry is not None and (info := self._info_of(entry)) is not None:
            command(info)

    def _conversation_chosen(self, entry: QListWidgetItem) -> None:
        match entry.data(KIND_ROLE):
            case "group":
                key = entry.data(Qt.ItemDataRole.UserRole)
                self._collapsed.symmetric_difference_update({key})
                self._render_sidebar()
            case "session":
                self._session_chosen(entry.data(Qt.ItemDataRole.UserRole))
            case _:
                conversation_id = entry.data(Qt.ItemDataRole.UserRole)
                own = self.home.model.conversation
                if (self._viewing is not None or own is None
                        or conversation_id != own.id):
                    self.open_conversation(conversation_id)

    def conversation_menu(self, info: ConversationInfo) -> QMenu:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        menu.addAction("Rename…", lambda: self.rename_conversation(info))
        go_on = menu.addAction("Continue in a new conversation",
                               lambda: self.open_conversation(None, seed_from=info.id))
        go_on.setToolTip("Start a new conversation that knows where this one left off.")
        menu.addAction("Unarchive" if info.archived else "Archive",
                       lambda: self.home.archive(info.id, not info.archived))
        menu.addSeparator()
        menu.addAction("Delete…", lambda: self.delete_conversation(info)).setVisible(
            self.home.can_delete)
        return menu

    def rename_conversation(self, info: ConversationInfo) -> None:
        dialog = QInputDialog(self)
        dialog.setWindowTitle("Rename conversation")
        dialog.setLabelText("New name (leave it empty to let Kayden name it):")
        dialog.setTextValue(info.title or "")
        dialog.textValueSelected.connect(lambda title: self.home.rename(info.id, title))
        dialog.open()

    def delete_conversation(self, info: ConversationInfo) -> None:
        box = QMessageBox(QMessageBox.Icon.NoIcon, "Delete conversation",
                          f"Delete “{conversation_label(info)}”?", parent=self)
        children = len(self.sessions.of(info.id, include_archived=True)) if self.sessions else 0
        box.setInformativeText(delete_details(self.home.is_open(info.id), children))
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        delete = box.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
        delete.setProperty("danger", True)
        delete.style().polish(delete)
        box.setDefaultButton(cancel)
        box.buttonClicked.connect(lambda button: button is delete
                                  and self.home.delete(info.id))
        box.open()

    def _add_to_app_menu(self) -> None:
        def added(_: None) -> None:
            self.app_menu_action.setVisible(False)
            self.model.notify("Kayden is now in your app menu.")

        self.controller.command(
            asyncio.to_thread(app_menu.install), done=added,
            failed=lambda e: self.model.notify(f"Kayden could not be added to the app menu: {e}"))

    def open_memory(self) -> None:
        self.open_settings("Memory")

    def _notice_link(self, link: str) -> None:
        if link == "memory":
            self.open_memory()

    def bring_forward(self) -> None:
        """Another launch of Kayden asked for this window."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _sessions_changed(self) -> None:
        self._refresh_status()
        self._render_sidebar()
        self._deliver_notes()

    def _deliver_notes(self) -> None:
        """Tell this window's conversation what its sub-sessions did while nobody waited:
        a notice as a message, which wakes the agent (queued while it works)."""
        own = self.home.model.conversation
        if self.sessions is not None and own is not None:
            for note in self.sessions.take_notes(own.id):
                self.home.send(note)

    def _session_of(self, session_id: str) -> SubSession | None:
        return next((s for s in (self.sessions.sessions if self.sessions else ())
                     if s.id == session_id), None)

    def _session_chosen(self, session_id: str) -> None:
        session = self._session_of(session_id)
        if session is None or (self._viewing is not None and self._viewing[0] is session) or (
                self._opening is not None and self._opening.session is session):
            return
        self.open_session(session)

    def _row_menu(self, entry: QListWidgetItem, at: QPoint) -> None:
        if entry.data(KIND_ROLE) == "session":
            if (session := self._session_of(entry.data(Qt.ItemDataRole.UserRole))) is not None:
                self.session_menu(session).popup(at)
        elif (info := self._info_of(entry)) is not None:
            self.conversation_menu(info).popup(at)

    def session_menu(self, session: SubSession) -> QMenu:
        assert self.sessions is not None
        sessions = self.sessions
        menu = QMenu(self)
        menu.addAction("Open", lambda: self.open_session(session))
        if session.resumable:
            menu.addAction("Resume", lambda: self.home.command(sessions.resume(session)))
        if session.running:
            menu.addAction("Stop", lambda: sessions.stop(session))
        menu.addAction("Change spending limit…", lambda: self.change_session_limit(session))
        menu.addAction("Rename…", lambda: self.rename_session(session))
        menu.addAction("Unarchive" if session.archived else "Archive",
                       lambda: sessions.archive(session, not session.archived))
        menu.addAction("Delete…", lambda: self.delete_session(session))
        return menu

    def change_session_limit(self, session: SubSession) -> None:
        assert self.sessions is not None
        sessions = self.sessions
        dialog = QInputDialog(self)
        dialog.setWindowTitle("Sub-session spending limit")
        dialog.setLabelText(f"Most “{clip(session.name, 60)}” may spend, in US dollars "
                            f"(${session.spent:.2f} spent so far):")
        dialog.setInputMode(QInputDialog.InputMode.DoubleInput)
        dialog.setDoubleDecimals(2)
        dialog.setDoubleRange(0.01, 1_000_000)
        dialog.setDoubleValue(session.budget_usd)

        def chosen(usd: float) -> None:
            def apply() -> None:
                self.home.command(sessions.set_child_limit(session, usd))

            if usd > session.budget_usd:
                confirm(self, "Raise this sub-session's limit?", apply, yes="Raise",
                        details=f"It may then spend up to ${usd:g}.")
            else:
                apply()

        dialog.doubleValueSelected.connect(chosen)
        dialog.open()

    def rename_session(self, session: SubSession) -> None:
        assert self.sessions is not None
        dialog = QInputDialog(self)
        dialog.setWindowTitle("Rename sub-session")
        dialog.setLabelText("New name (leave it empty to show its task):")
        dialog.setTextValue(session.title or "")
        dialog.textValueSelected.connect(lambda title: self.sessions.rename(session, title))
        dialog.open()

    def delete_session(self, session: SubSession) -> None:
        assert self.sessions is not None
        sessions = self.sessions

        def delete() -> None:
            if self._viewing is not None and self._viewing[0] is session:
                self._leave_session()
            self.home.command(sessions.delete(session))

        confirm(self, f"Delete the sub-session “{clip(session.name, 80)}”?", delete,
                yes="Delete", danger=True,
                details="It stops, and its conversation is erased. This cannot be undone.")

    def open_session(self, session: SubSession) -> None:
        """Show a sub-session in the conversation area, as its own conversation: its
        messages, steps and prompts, and the message box talks to it. Its row stays
        highlighted while it opens, which may mean starting it again."""
        opening = self._open(session=session)
        self._render_sidebar()
        _spawn(self._open_session(opening, session))

    async def _open_session(self, opening: Opening, session: SubSession) -> None:
        assert self.sessions is not None
        try:
            remote = await self.sessions.open(session)
        except Exception as e:  # noqa: BLE001 - shown, never fatal
            self._settle(opening)
            self.model.notify(f"The sub-session could not be opened: {user_message(e)}")
            return
        if self._opening is not opening:  # the user asked for something else meanwhile
            self.sessions.release(session, remote)
            return
        self._leave_session()
        controller = Controller(remote, ConversationModel())
        pump = asyncio.create_task(controller.run())
        self._viewing = (session, remote, pump)
        self._opening = None
        self._show(controller)

    def _leave_session(self) -> None:
        """Back to this window's own conversation; the sub-session keeps working."""
        if self._viewing is None:
            return
        session, remote, pump = self._viewing
        self._viewing = None
        pump.cancel()
        assert self.sessions is not None
        self.sessions.release(session, remote)
        self._show(self.home)

    def _show(self, controller: Controller) -> None:
        self.model.on_change = lambda change: None
        self.controller = controller
        self.model = controller.model
        self.model.on_change = self._changed
        self._rebuild()
        self._render_sidebar()

    def open_duty(self) -> None:
        if self.model.conversation is None:
            self.model.notify("Open a conversation first.")
            return
        DutyDialog(self, self.controller, self.model.conversation).open()

    def open_tool_servers(self) -> None:
        self.open_settings("Tool servers")

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
        self._refresh_status()  # the model's name, now that it is known

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

    def open_settings(self, page: str | None = None) -> None:
        """The Settings window, at `page` (a section's name) or its first."""
        async def load() -> None:
            settings = await self.runtime.settings()
            try:
                models = await self.runtime.list_models()
            except Exception:  # noqa: BLE001 - e.g. no key yet; the provider can still change
                models = []
            spent = self.runtime.snapshot().usage.usage.cost_usd or 0.0
            dialog = SettingsDialog(self, self.runtime, self.home, settings, models, spent,
                                    self.sessions)
            self._dialog = dialog
            self.memory_dialog, self.mcp_dialog = dialog.memory, dialog.servers
            if page is not None:
                dialog.show_page(page)
            dialog.open()

        self.controller.command(load())

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closed.emit()
        super().closeEvent(event)


# ── long-term memory ──────────────────────────────────────────────────────


class MemoryDialog(QDialog):
    """What Kayden remembers across conversations: the profile and notes, which the user
    adds, edits and deletes."""

    def __init__(self, parent: QWidget, controller: Controller, *, embedded: bool = False
                 ) -> None:
        super().__init__(parent)
        self.controller = controller
        if embedded:  # a page of Settings
            self.setWindowFlags(Qt.WindowType.Widget)
        self.setWindowTitle("Memory")
        self.setMinimumSize(560, 460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*((4, 16, 18, 4) if embedded else (20, 18, 20, 16)))
        layout.addWidget(_label("Memory", selectable=False,
                                role="title" if embedded else "heading"))
        layout.addWidget(_label(MEMORY_HINT, tone="muted"))
        self.entries = QListWidget()
        self.entries.setAccessibleName("Memory notes")
        self.entries.setItemDelegate(RowDelegate(self.entries, wrap=True))
        self.entries.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.entries.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.entries.setMouseTracking(True)
        self.entries.currentItemChanged.connect(lambda *_: self._selection_changed())
        self.entries.itemActivated.connect(lambda _: self._edit())
        layout.addWidget(self.entries, 1)
        self.add_button = _primary(QPushButton("Add…"))
        self.add_button.clicked.connect(lambda: NoteDialog(self, None).open())
        self.edit_button = QPushButton("Edit…")
        self.edit_button.clicked.connect(self._edit)
        self.forget_button = QPushButton("Delete…")
        self.forget_button.clicked.connect(self._forget)
        row = QHBoxLayout()
        for button in (self.add_button, self.edit_button, self.forget_button):
            row.addWidget(button)
        row.addStretch()
        if not embedded:
            close = QPushButton("Close")
            close.clicked.connect(self.accept)
            row.addWidget(close)
        layout.addLayout(row)
        self._selection_changed()

    def refresh(self) -> None:
        async def load() -> None:
            notes = await self.controller.notes()
            selected = self._selected()
            self.entries.clear()
            for note in notes or ():
                item = QListWidgetItem(note.title)
                item.setData(Qt.ItemDataRole.UserRole, note)
                item.setData(PREVIEW_ROLE, note.description)
                item.setToolTip(memory_source(note))
                self.entries.addItem(item)
                if selected is not None and note.id == selected.id:
                    self.entries.setCurrentItem(item)
            self._selection_changed()

        self.controller.command(load())

    def _selected(self) -> MemoryNote | None:
        item = self.entries.currentItem()
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _selection_changed(self) -> None:
        note = self._selected()
        self.edit_button.setEnabled(note is not None)
        self.forget_button.setEnabled(note is not None and not note.fixed)

    def _edit(self) -> None:
        if (note := self._selected()) is not None:
            NoteDialog(self, note).open()

    def _forget(self) -> None:
        if (note := self._selected()) is not None:
            confirm(self, f"Delete the note “{clip(note.title, 80)}”?",
                    lambda: self.controller.delete_note(note.id), yes="Delete", danger=True,
                    details="Kayden won't use it again. The messages it came from stay until "
                            "you delete their conversation.")


class NoteDialog(QDialog):
    """Add or edit one memory note; the profile keeps its title."""

    def __init__(self, parent: MemoryDialog, note: MemoryNote | None) -> None:
        super().__init__(parent)
        self.memory, self.note = parent, note
        self.setWindowTitle("Edit note" if note else "Add a note")
        self.setMinimumSize(560, 420)
        form = QFormLayout(self)
        form.setContentsMargins(20, 18, 20, 16)
        self.title = QLineEdit(note.title if note else "")
        self.title.setPlaceholderText("A project, person or topic")
        self.title.setReadOnly(note is not None and note.fixed)
        self.description = QLineEdit(note.description if note else "")
        self.description.setPlaceholderText("One line: what the note is about")
        self.text = QPlainTextEdit(note.text if note else "")
        self.error = _label()
        theme.mark(self.error, "tone", "error")
        self.error.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.save_button = _primary(buttons.button(QDialogButtonBox.StandardButton.Save))
        form.addRow("Title", self.title)
        form.addRow("About", self.description)
        form.addRow("Note", self.text)
        form.addRow(self.error)
        form.addRow(buttons)

    def _save(self) -> None:
        self.save_button.setEnabled(False)

        def failed(error: Exception) -> None:
            self.save_button.setEnabled(True)
            self.error.setText(user_message(error))
            self.error.show()

        self.memory.controller.save_note(
            self.title.text(), self.description.text(), self.text.toPlainText(),
            self.note.id if self.note else None, failed=failed,
            done=lambda _: self.accept())


class DutyDialog(QDialog):
    """The open conversation's duty and plan."""

    def __init__(self, parent: QWidget, controller: Controller, info: ConversationInfo) -> None:
        super().__init__(parent)
        self.controller = controller
        duty = info.duty or Duty("")
        self.setWindowTitle("Duty")
        self.setMinimumSize(600, 520)
        form = QFormLayout(self)
        form.setContentsMargins(20, 18, 20, 16)
        form.addRow(_label("Duty", selectable=False, role="heading"))
        form.addRow(_label(DUTY_HINT, tone="muted"))
        self.instruction = QPlainTextEdit(duty.instruction)
        self.instruction.setPlaceholderText("e.g. Keep my inbox answered")
        self.instruction.setMaximumHeight(90)
        self.kind = QComboBox()
        for kind, label in DUTY_KIND_LABELS.items():
            self.kind.addItem(label, kind)
        self.kind.setCurrentIndex(self.kind.findData(duty.kind))
        self.max_minutes, self.min_minutes = QDoubleSpinBox(), QDoubleSpinBox()
        for box, seconds in ((self.max_minutes, duty.max_interval_s),
                             (self.min_minutes, duty.min_interval_s)):
            box.setRange(0.1, 7 * 24 * 60)
            box.setDecimals(1)
            box.setSuffix(" minutes")
            box.setValue(seconds / 60)
        self.plan = QPlainTextEdit(plan_text(info.plan))
        self.plan.setPlaceholderText("[todo] A step | due 2026-11-12 09:00 | a note")
        self.error = _label()
        theme.mark(self.error, "tone", "error")
        self.error.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.save_button = _primary(buttons.addButton(
            "Save" if info.duty else "Turn on", QDialogButtonBox.ButtonRole.AcceptRole))
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        if info.duty is not None:
            off = buttons.addButton("Turn off", QDialogButtonBox.ButtonRole.DestructiveRole)
            off.clicked.connect(lambda: self._apply(None))
        form.addRow("What to do", self.instruction)
        form.addRow("How long", self.kind)
        form.addRow("Check in at least every", self.max_minutes)
        form.addRow("and at most every", self.min_minutes)
        form.addRow(_label(PLAN_HINT, tone="muted"))
        form.addRow("Plan", self.plan)
        form.addRow(self.error)
        form.addRow(buttons)

    def _save(self) -> None:
        instruction = self.instruction.toPlainText().strip()
        self._apply(Duty(instruction, self.kind.currentData(), self.min_minutes.value() * 60,
                         self.max_minutes.value() * 60) if instruction else None)

    def _fail(self, error: Exception) -> None:
        self.save_button.setEnabled(True)
        self.error.setText(user_message(error))
        self.error.show()

    def _apply(self, duty: Duty | None) -> None:
        try:
            plan = parse_plan_text(self.plan.toPlainText())
        except ValueError as e:
            self._fail(e)
            return
        self.save_button.setEnabled(False)
        self.controller.save_duty(duty, plan, failed=self._fail, done=lambda _: self.accept())


class McpDialog(QDialog):
    """The MCP servers whose tools Kayden offers. Changes are saved at once and start with
    the next start, which Restart now brings."""

    def __init__(self, parent: QWidget, runtime: RuntimeAPI, controller: Controller, *,
                 embedded: bool = False) -> None:
        super().__init__(parent)
        self.runtime, self.controller = runtime, controller
        if embedded:  # a page of Settings
            self.setWindowFlags(Qt.WindowType.Widget)
        self.saved: list[McpServer] = []
        self._changes = 0  # saves made here; a list read before the latest one is stale
        self.setWindowTitle("Tool servers")
        self.setMinimumSize(520, 400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*((4, 16, 18, 4) if embedded else (20, 18, 20, 16)))
        layout.addWidget(_label("Tool servers (MCP)", selectable=False,
                                role="title" if embedded else "heading"))
        layout.addWidget(_label("Each server gives Kayden more tools, such as GitHub, a "
                                "database or your calendar.", tone="muted"))
        self.servers = QListWidget()
        self.servers.setAccessibleName("Tool servers")
        self.servers.setItemDelegate(RowDelegate(self.servers))
        self.servers.currentItemChanged.connect(lambda *_: self._selection_changed())
        self.servers.itemActivated.connect(lambda _: self._edit())
        self.empty = _label("No servers yet.", selectable=False, tone="muted")
        self.empty.hide()  # until the list has loaded
        layout.addWidget(self.servers, 1)
        layout.addWidget(self.empty)
        self.notice = _label("", selectable=False, tone="warning")
        self.notice.hide()
        layout.addWidget(self.notice)
        self.add_button = _primary(QPushButton("Add…"))
        self.add_button.clicked.connect(self._add)
        self.edit_button = QPushButton("Edit…")
        self.edit_button.clicked.connect(self._edit)
        self.remove_button = QPushButton("Remove…")
        self.remove_button.clicked.connect(self._remove)
        self.restart_button = QPushButton("Restart now")
        self.restart_button.clicked.connect(
            lambda: (self.accept(), self.controller.command(self.runtime.restart())))
        self.restart_button.hide()
        row = QHBoxLayout()
        for button in (self.add_button, self.edit_button, self.remove_button):
            row.addWidget(button)
        row.addStretch()
        row.addWidget(self.restart_button)
        if not embedded:
            close = QPushButton("Close")
            close.clicked.connect(self.accept)
            row.addWidget(close)
        layout.addLayout(row)
        self._selection_changed()

    def refresh(self) -> None:
        changes = self._changes
        self.controller.command(self.runtime.settings(),
                                done=lambda s: changes == self._changes and self._show(s))

    def _show(self, settings: Settings) -> None:
        self.saved = list(settings.mcp_servers)
        self.servers.clear()
        for server in self.saved:
            item = QListWidgetItem(server.name)
            item.setData(Qt.ItemDataRole.UserRole, server)
            item.setData(PREVIEW_ROLE, mcp_target(server))
            self.servers.addItem(item)
        self.empty.setVisible(not self.saved)
        notice = restart_notice(settings) if settings.restart_needed else None
        self.notice.setText(notice or "")
        self.notice.setVisible(notice is not None)
        self.restart_button.setVisible(settings.restart_needed)
        self._selection_changed()

    def _selected(self) -> McpServer | None:
        item = self.servers.currentItem()
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _selection_changed(self) -> None:
        for button in (self.edit_button, self.remove_button):
            button.setEnabled(self._selected() is not None)

    async def save(self, servers: list[McpServer]) -> Settings:
        self._changes += 1
        return await self.runtime.update_settings(SettingsUpdate(mcp_servers=servers))

    def _add(self) -> None:
        ServerDialog(self, None).open()

    def _edit(self) -> None:
        if (server := self._selected()) is not None:
            ServerDialog(self, server).open()

    def _remove(self) -> None:
        if (server := self._selected()) is not None:
            rest = [s for s in self.saved if s.name != server.name]
            confirm(self, f"Remove the tool server “{server.name}”?",
                    lambda: self.controller.command(self.save(rest), done=self._show),
                    yes="Remove", danger=True,
                    details="Its tools are gone after the next restart, and the keys saved "
                            "for it are deleted.")


class ServerDialog(QDialog):
    """Add or edit one server; saved through the runtime, which checks it."""

    def __init__(self, parent: McpDialog, server: McpServer | None) -> None:
        super().__init__(parent)
        self.list, self.original = parent, server
        self.setWindowTitle("Edit tool server" if server else "Add tool server")
        self.setMinimumWidth(520)
        form = QFormLayout(self)
        form.setContentsMargins(20, 18, 20, 16)
        self.name = QLineEdit(server.name if server else "")
        self.name.setPlaceholderText("github")
        self.target = QLineEdit(mcp_target(server) if server else "")
        self.target.setPlaceholderText("npx -y @modelcontextprotocol/server-github, or a URL")
        self.variables = QPlainTextEdit(mcp_variables(server) if server else "")
        self.variables.setPlaceholderText("GITHUB_TOKEN=…")
        self.variables.setMaximumHeight(90)
        self.warning = _label(MCP_WARNING, tone="warning")
        self.error = _label()
        theme.mark(self.error, "tone", "error")
        self.error.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.save_button = _primary(buttons.button(QDialogButtonBox.StandardButton.Save))
        form.addRow("Name", self.name)
        form.addRow("Command or URL", self.target)
        form.addRow("Variables", self.variables)
        form.addRow("", _label(MCP_VARIABLES_HINT, selectable=False, role="small"))
        form.addRow(self.warning)
        form.addRow(self.error)
        form.addRow(buttons)

    def _save(self) -> None:
        try:
            server = mcp_server(self.name.text(), self.target.text(),
                                self.variables.toPlainText())
        except ValueError as e:
            self._show_error(str(e))
            return
        servers = [server if self.original and s.name == self.original.name else s
                   for s in self.list.saved]
        if self.original is None:
            servers.append(server)
        self.save_button.setEnabled(False)

        def saved(settings: Settings) -> None:
            self.accept()
            self.list._show(settings)

        def failed(error: Exception) -> None:
            self.save_button.setEnabled(True)
            self._show_error(user_message(error))

        self.list.controller.command(self.list.save(servers), done=saved, failed=failed)

    def _show_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()


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
                 settings: Settings, models: Sequence[ModelInfo], spent: float,
                 sessions: SubSessions | None = None) -> None:
        super().__init__(parent)
        self.runtime, self.controller, self.settings = runtime, controller, settings
        self.sessions = sessions
        self.setWindowTitle("Settings")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setObjectName("settings")
        self.resize(900, 640)

        self.startup_error = _label(f"Kayden could not start: {settings.startup_error}")
        theme.mark(self.startup_error, "tone", "error")
        self.startup_error.setVisible(settings.startup_error is not None)
        self.modules = {kind: self._module_box(kind) for kind in MODULE_LABELS
                        if kind in settings.modules}
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
        self.child_limit = QLineEdit(f"{sessions.limit_usd:g}" if sessions else "")
        self.child_limit.setToolTip("What a sub-session may spend unless the agent gives it "
                                    "less. It is not part of your own limit.")
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

        self.memory = MemoryDialog(self, controller, embedded=True)
        self.servers = McpDialog(self, runtime, controller, embedded=True)
        self.pages: dict[str, QWidget] = {
            "General": _page(
                "General", self.startup_error,
                _setting("AI provider", "Which company's models Kayden uses.",
                         self.modules["core"]),
                _setting("API key", "Your key for that provider, kept in the system keychain.",
                         self.api_key),
                _setting("Model", "Writes the answers.", self.model),
                _setting("Model for memory upkeep", "Summarizes and saves memory; a cheaper "
                         "one is fine.", self.light),
                self.model_note,
                *(_setting(MODULE_LABELS[kind], MODULE_HINTS[kind], self.modules[kind])
                  for kind in ("tools", "frontend")),
                _label(AT_RESTART, selectable=False, role="small")),
            "Spending": _page(
                "Spending", _setting("Spending limit (US$)", f"Kayden stops when it reaches it. "
                         f"{dollars(spent)} spent so far.", self.limit),
                *((_setting("Limit per sub-session (US$)", "What a sub-session may spend "
                            "unless the agent gives it less; not part of your own limit.",
                            self.child_limit),) if sessions is not None else ())),
            "Permissions": _page(
                "Permissions", _setting("What the agent may do", "When it asks you before acting.",
                         self.permissions),
                _setting("Folder for new chats", "Where new conversations work.",
                         folder_row)),
            "Instructions": _page(
                "Instructions", _setting("Instructions", "What Kayden is told at the start of every "
                         "conversation.", self.instructions, below=True)),
            "Memory": self.memory,
            "Tool servers": self.servers,
        }
        self.nav = QListWidget(objectName="settingsNav")
        self.nav.setFixedWidth(190)
        self.stack = QStackedWidget()
        for name, page in self.pages.items():
            item = QListWidgetItem(name)
            item.setSizeHint(QSize(0, 34))
            self.nav.addItem(item)
            self.stack.addWidget(page)
        self.nav.currentRowChanged.connect(self._page_changed)
        self.nav.setCurrentRow(0)

        header = DragArea(objectName="settingsHeader")
        top = QHBoxLayout(header)
        top.setContentsMargins(18, 10, 10, 10)
        top.addWidget(_label("Settings", selectable=False, role="title"), 1)
        close = QToolButton(objectName="windowControl")
        close.setIcon(theme.icon("close", theme.current().muted))
        close.setToolTip("Close")
        close.setAccessibleName("Close")
        close.clicked.connect(self.reject)
        top.addWidget(close)
        footer = QHBoxLayout()
        footer.setContentsMargins(18, 8, 18, 14)
        notes = QVBoxLayout()
        notes.addWidget(self.notice)
        notes.addWidget(self.error)
        footer.addLayout(notes, 1)
        footer.addWidget(buttons)
        body = QHBoxLayout()
        body.setContentsMargins(12, 0, 0, 0)
        body.setSpacing(12)
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(header)
        outer.addLayout(body, 1)
        outer.addLayout(footer)

    def show_page(self, name: str) -> None:
        self.nav.setCurrentRow(list(self.pages).index(name))

    def _page_changed(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        page = self.stack.currentWidget()
        if page is self.memory or page is self.servers:
            page.refresh()

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
            child_limit = self._child_limit()
        except ValueError as e:
            self._show_error(str(e))
            return
        if update == SettingsUpdate() and child_limit is None and not restart:
            self.accept()
            return
        reasons = loosening(self.settings, update)
        if self.sessions is not None and child_limit and child_limit > self.sessions.limit_usd:
            reasons.append("raises the limit per sub-session")
        if reasons:
            confirm(self, "Save these changes?",
                    lambda: self._apply(update, restart, child_limit),
                    yes="Save", details="This change " + " and ".join(reasons) + ".")
        else:
            self._apply(update, restart, child_limit)

    def _child_limit(self) -> float | None:
        """The new limit per sub-session, None when unchanged."""
        if self.sessions is None:
            return None
        try:
            usd = float(self.child_limit.text().strip().lstrip("$"))
        except ValueError:
            raise ValueError("Write the limit per sub-session as a number of dollars.") from None
        if usd <= 0:
            raise ValueError("The limit per sub-session must be more than $0.")
        return None if usd == self.sessions.limit_usd else usd

    def _apply(self, update: SettingsUpdate, restart: bool,
               child_limit: float | None = None) -> None:
        self.save_button.setEnabled(False)
        self.error.hide()

        async def apply() -> Settings:
            settings = self.settings
            if child_limit is not None and self.sessions is not None:
                await self.sessions.set_limit(child_limit)
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
        # Rows are sized by the style sheet, and the list lays them out as soon as one is
        # selected: without this they are placed by their unstyled height and overlap.
        self.provider.ensurePolished()
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
        column.addWidget(_label("Kayden works freely in its work folder and asks before "
                                "anything outside it. You can change that, and everything "
                                "here, in Settings."))
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

