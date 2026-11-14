"""The desktop app's look: one palette per system color scheme, a Qt style sheet built
from it, and the few icons the app draws itself.

Widgets pick their styling with object names and dynamic properties (`primary`, `tone`,
`role`); custom-painted widgets read `current()` when they paint, so a change of the
system's light or dark setting restyles everything. The zoom scales the app font and every
font size in the style sheet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget


@dataclass(frozen=True)
class Palette:
    dark: bool
    bg: str  # the conversation and dialogs
    side: str  # the sidebar
    card: str  # tool cards and the header group
    field: str  # text fields and the message box
    line: str  # hairlines and outlines
    track: str  # an off switch
    text: str
    muted: str  # secondary text
    accent: str
    accent_hover: str
    accent_soft: str  # selection and the empty-chat avatar
    accent_text: str  # text on accent_soft
    on_accent: str  # text on accent
    ok: str
    warning: str
    error: str
    error_bg: str
    error_text: str
    bubble: str  # your messages
    select: str  # the open conversation in the sidebar
    composer: str  # the message box


LIGHT = Palette(
    dark=False, bg="#FFFFFF", side="#F5F5F7", card="#F5F5F7", field="#FFFFFF",
    line="#E3E3E8", track="#C9C9D2", text="#1B1B1F", muted="#6C6C78", accent="#5B5BD6",
    accent_hover="#4C4CC4", accent_soft="#EDEDFC", accent_text="#3E3EA8", on_accent="#FFFFFF",
    ok="#1F8A5B", warning="#9A5B00", error="#C62828", error_bg="#FDECEC",
    error_text="#8E1B1B", bubble="#EFEFF3", select="#E7E7EC", composer="#F4F4F7")
DARK = Palette(
    dark=True, bg="#1A1A1E", side="#131316", card="#232328", field="#202025",
    line="#2F2F36", track="#4A4A55", text="#ECECF0", muted="#9A9AA6", accent="#8C8CF5",
    accent_hover="#A3A3F8", accent_soft="#2A2A48", accent_text="#C9C9FF", on_accent="#14142B",
    ok="#4CC38A", warning="#F3C773", error="#F28B82", error_bg="#3B1F22",
    error_text="#F7B4AE", bubble="#2B2B31", select="#26262C", composer="#26262C")

ICONS = Path(__file__).parent / "icons"

_current = LIGHT
THEMES = ("system", "light", "dark")  # "system" follows the desktop's color scheme
_choice = "system"
ZOOM_LEVELS = (0.67, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)
_zoom = 1.0
_base_pt: float | None = None  # the app font's size before any zoom


def current() -> Palette:
    return _current


def choice() -> str:
    """The theme the user chose (THEMES)."""
    return _choice


def set_theme(app: QApplication, name: str) -> None:
    global _choice
    _choice = name if name in THEMES else "system"
    apply(app)


def system_is_dark() -> bool:
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def zoom() -> float:
    return _zoom


def zoom_step(level: float, steps: int) -> float:
    """The level `steps` places up (positive) or down from `level`, stopping at the ends."""
    if steps > 0:
        return next((z for z in ZOOM_LEVELS if z > level), ZOOM_LEVELS[-1])
    return next((z for z in reversed(ZOOM_LEVELS) if z < level), ZOOM_LEVELS[0])


def set_zoom(app: QApplication, level: float) -> None:
    global _zoom
    _zoom = min(max(level, ZOOM_LEVELS[0]), ZOOM_LEVELS[-1])
    apply(app, _current.dark)


def apply(app: QApplication, dark: bool | None = None) -> None:
    """Style the app for `dark`, or for the chosen theme when None, at the zoom."""
    global _current, _base_pt
    if dark is None:
        dark = system_is_dark() if _choice == "system" else _choice == "dark"
    _current = DARK if dark else LIGHT
    if _base_pt is None:
        _base_pt = app.font().pointSizeF()
    font = QFont(app.font())
    font.setPointSizeF(_base_pt * _zoom)
    app.setFont(font)
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setPalette(qt_palette(_current))
    app.setStyleSheet(style_sheet(_current, _base_pt * _zoom))


def follow_system(app: QApplication) -> None:
    """Restyle whenever the system switches between light and dark; a chosen light or
    dark theme stays."""
    app.styleHints().colorSchemeChanged.connect(lambda _: apply(app))


def qt_palette(c: Palette) -> QPalette:
    palette = QPalette()
    roles = QPalette.ColorRole
    for role, color in ((roles.Window, c.bg), (roles.WindowText, c.text), (roles.Base, c.field),
                        (roles.AlternateBase, c.card), (roles.Text, c.text),
                        (roles.Button, c.bg), (roles.ButtonText, c.text),
                        (roles.Highlight, c.accent), (roles.HighlightedText, c.on_accent),
                        (roles.ToolTipBase, c.bg), (roles.ToolTipText, c.text),
                        (roles.PlaceholderText, c.muted), (roles.Link, c.accent),
                        (roles.Mid, c.line), (roles.Midlight, c.card), (roles.Light, c.bg),
                        (roles.Dark, c.track), (roles.Shadow, c.track)):
        palette.setColor(role, QColor(color))
    for role in (roles.WindowText, roles.Text, roles.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(c.muted))
    return palette


def style_sheet(c: Palette, base_pt: float) -> str:
    small, side, title, heading = (f"{base_pt * f:.1f}pt" for f in (0.85, 0.92, 1.15, 1.45))
    body = f"{base_pt:.1f}pt"
    chevron = (ICONS / f"chevron-{'dark' if c.dark else 'light'}.svg").as_posix()
    return f"""
QMainWindow, QDialog {{ background: {c.bg}; }}
QWidget#sidebar {{ background: {c.side}; }}
QSplitter::handle {{ background: {c.line}; }}
QScrollArea, QWidget#transcript {{ background: {c.bg}; border: none; }}
QToolTip {{ background: {c.bg}; color: {c.text}; border: 1px solid {c.line}; padding: 4px; }}

QLabel[tone="muted"], QLabel:disabled {{ color: {c.muted}; }}
QLabel[tone="ok"] {{ color: {c.ok}; }}
QLabel[tone="warning"] {{ color: {c.warning}; }}
QLabel[tone="error"] {{ color: {c.error}; }}
QLabel[role="strong"] {{ font-weight: 600; }}
QLabel[role="small"] {{ color: {c.muted}; font-size: {small}; }}
QLabel[role="title"] {{ font-size: {title}; font-weight: 600; }}
QLabel[role="heading"] {{ font-size: {heading}; font-weight: 600; }}

QPushButton {{ background: {c.bg}; border: 1px solid {c.line}; border-radius: 8px;
    padding: 6px 12px; }}
QPushButton:hover {{ background: {c.card}; }}
QPushButton:pressed {{ background: {c.line}; }}
QPushButton:focus {{ border-color: {c.accent}; }}
QPushButton:disabled {{ color: {c.muted}; }}
QPushButton[primary="true"] {{ background: {c.accent}; color: {c.on_accent};
    border-color: {c.accent}; font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: {c.accent_hover}; }}
QPushButton[primary="true"]:disabled {{ background: {c.accent_soft}; color: {c.accent_text};
    border-color: {c.accent_soft}; }}
QMessageBox QLabel#qt_msgbox_label {{ font-weight: 600; }}
QMessageBox QLabel#qt_msgbox_informativelabel {{ min-width: 320px; }}
QPushButton[danger="true"] {{ background: {c.error}; color: {c.bg}; border-color: {c.error};
    font-weight: 600; }}
QToolButton {{ background: transparent; border: none; border-radius: 6px; padding: 4px 6px;
    color: {c.muted}; }}
QToolButton:hover {{ background: {c.card}; color: {c.text}; }}
QLabel#reasoning {{ border: none; border-left: 2px solid {c.track}; padding: 2px 0 2px 12px; }}
QProgressBar {{ background: {c.line}; border: none; border-radius: 3px; }}
QProgressBar::chunk {{ background: {c.accent}; border-radius: 3px; }}
QFrame#attachment {{ background: {c.bg}; border: 1px solid {c.line}; border-radius: 8px; }}
QToolButton#removeAttachment {{ background: transparent; border-radius: 9px; padding: 0; }}
QToolButton#removeAttachment:hover {{ background: {c.select}; }}
QToolButton#send {{ background: transparent; border-radius: 8px; padding: 0; }}
QToolButton#send:hover {{ background: {c.select}; }}
QToolButton#send[active="true"] {{ background: {c.accent}; }}
QToolButton#send[active="true"]:hover {{ background: {c.accent_hover}; }}
QToolButton#modelPicker {{ color: {c.muted}; padding: 2px 6px; font-size: {small}; }}
QToolButton#modelPicker:hover {{ background: {c.select}; color: {c.text}; }}
QToolButton#modelPicker::menu-indicator {{ image: none; width: 0; }}
QToolButton#folderPicker {{ color: {c.text}; padding: 8px 14px; border: 1px solid {c.line};
    border-radius: 10px; }}
QToolButton#folderPicker:hover {{ background: {c.select}; }}
QToolButton#folderPicker::menu-indicator {{ image: none; width: 0; }}
QMenu#folderMenu {{ min-width: 260px; padding: 6px; }}
QMenu#folderMenu::item {{ padding: 8px 28px 8px 12px; border-radius: 6px; }}
QToolButton#folder {{ padding: 0 4px; }}
QToolButton#folder:hover {{ background: {c.line}; color: {c.text}; }}

QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {{ background: {c.field}; color: {c.text};
    border: 1px solid {c.line}; border-radius: 8px; padding: 5px 8px;
    selection-background-color: {c.accent}; selection-color: {c.on_accent}; }}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {c.accent}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {c.muted}; background: {c.card}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({chevron}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {c.bg}; border: 1px solid {c.line};
    selection-background-color: {c.accent_soft}; selection-color: {c.accent_text}; }}

QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget#choices::item {{ border: 1px solid {c.line}; border-radius: 8px;
    padding: 8px 10px; margin: 2px 0; }}
QListWidget#choices::item:selected {{ border-color: {c.accent}; background: {c.accent_soft};
    color: {c.accent_text}; }}

QMenuBar {{ background: {c.bg}; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 6px; }}
QMenuBar::item:selected {{ background: {c.card}; }}
QMenu {{ background: {c.bg}; border: 1px solid {c.line}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {c.accent_soft}; color: {c.accent_text}; }}
QMenu::item:disabled {{ color: {c.muted}; }}
QMenu::separator {{ height: 1px; background: {c.line}; margin: 4px 8px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {c.track}; border: 2px solid transparent;
    border-radius: 5px; background-clip: padding; min-height: 32px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QFrame#header {{ border: none; }}
QPushButton#nav {{ background: transparent; border: none; color: {c.text};
    padding: 7px 10px; text-align: left; font-size: {side}; border-radius: 8px; }}
QListWidget#conversations {{ font-size: {side}; }}
QLabel[role="header"] {{ font-size: {body}; font-weight: 600; }}
QPushButton#nav:hover {{ background: {c.select}; }}
QToolButton#menu {{ padding: 6px; border-radius: 8px; }}
QToolButton#menu:hover {{ background: {c.select}; }}
QToolButton#replyAction {{ padding: 3px; border-radius: 6px; }}
QToolButton#replyAction:hover {{ background: {c.select}; }}
QToolButton#codeCopy {{ padding: 3px; border-radius: 6px; background: transparent; }}
QToolButton#codeCopy:hover {{ background: {c.bubble}; }}
QToolButton#profile {{ color: {c.text}; padding: 6px 8px; }}
QToolButton#windowControl {{ padding: 6px; border-radius: 6px; }}
QDialog#settings {{ border: 1px solid {c.line}; }}
QFrame#settingsHeader {{ border-bottom: 1px solid {c.line}; }}
QFrame#settingRow {{ border-bottom: 1px solid {c.line}; }}
QWidget#settingsPage {{ background: {c.bg}; }}
QListWidget#settingsNav {{ margin-top: 12px; }}
QListWidget#settingsNav::item {{ padding: 0 10px; border-radius: 8px; }}
QListWidget#settingsNav::item:selected, QListWidget#settingsNav::item:hover {{
    background: {c.select}; color: {c.text}; }}
QToolButton#windowControl:hover {{ background: {c.select}; }}
QToolButton#titleControl, QToolButton#titleClose {{ padding: 0; border-radius: 0; }}
QToolButton#titleControl:hover {{ background: {c.select}; }}
QToolButton#titleClose:hover {{ background: {c.error}; }}
QToolButton#profile:hover, QToolButton#gear:hover {{ background: {c.select}; }}
QToolButton#profile::menu-indicator {{ image: none; width: 0; }}
QToolButton#menu::menu-indicator {{ image: none; width: 0; }}
QFrame#headerGroup {{ background: {c.card}; border-radius: 8px; }}
QFrame#banner {{ background: {c.error_bg}; border-radius: 8px; }}
QFrame#banner QLabel {{ color: {c.error_text}; }}
QFrame#composer {{ background: {c.composer}; border: 1px solid {c.line}; border-radius: 14px; }}
QFrame#composer[focused="true"] {{ border-color: {c.track}; }}
QFrame#composer QPlainTextEdit {{ background: transparent; border: none; padding: 0; }}
QFrame#user {{ background: {c.bubble}; border-radius: 14px; }}
QFrame#user QLabel {{ color: {c.text}; }}
QFrame#user:disabled {{ background: transparent; border: 1px dashed {c.line}; }}
QFrame#user QLabel:disabled {{ color: {c.muted}; }}
QFrame#tool {{ background: transparent; border: none; }}
QFrame#stepsHeader {{ border-radius: 6px; }}
QFrame#stepsHeader:hover {{ background: {c.card}; }}
QFrame#stepsBody {{ border-left: 2px solid {c.line}; margin-left: 9px; }}
QFrame#tool QPlainTextEdit {{ background: {c.bg}; }}
QFrame#prompt {{ background: {c.bg}; border: 1px solid {c.line};
    border-left: 3px solid {c.accent}; }}
QFrame#plan {{ background: {c.bg}; border: 1px solid {c.line}; border-radius: 10px; }}
QTextBrowser#reply {{ background: transparent; border: none; padding: 0; }}
"""


def mark(widget: QWidget, name: str, value: object) -> None:
    """Set a styling property and restyle the widget for it."""
    if widget.property(name) != value:
        widget.setProperty(name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def icon(kind: str, color: str, size: int = 16) -> QIcon:
    """A line icon: "send" (an up arrow), "stop" (a square), "folder", "gear", "menu" (three
    lines), "plus", "voice" (a sound wave), "copy", "check", "folded" and "unfolded" (chevrons), a plan step's "done", "todo" or "dropped", or a
    window control:
    "minimize", "maximize", "restore", "close"."""
    scale = 2
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), size / 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
               Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    s = size
    match kind:
        case "send":
            path = QPainterPath(QPointF(s * 0.5, s * 0.82))
            path.lineTo(s * 0.5, s * 0.18)
            path.moveTo(s * 0.22, s * 0.45)
            path.lineTo(s * 0.5, s * 0.18)
            path.lineTo(s * 0.78, s * 0.45)
            painter.drawPath(path)
        case "stop":
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(s * 0.3, s * 0.3, s * 0.4, s * 0.4), 1.5, 1.5)
        case "folder":
            path = QPainterPath(QPointF(s * 0.12, s * 0.78))
            path.lineTo(s * 0.12, s * 0.24)
            path.lineTo(s * 0.4, s * 0.24)
            path.lineTo(s * 0.5, s * 0.36)
            path.lineTo(s * 0.88, s * 0.36)
            path.lineTo(s * 0.88, s * 0.78)
            path.closeSubpath()
            painter.drawPath(path)
        case "gear":  # eight square teeth around a ring, and the hole
            painter.setPen(QPen(QColor(color), size / 11, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            centre = QPointF(s / 2, s / 2)
            outer, inner, teeth = s * 0.44, s * 0.33, 8
            path = QPainterPath()
            for i in range(teeth * 4):
                angle = math.radians(i * 360 / (teeth * 4) - 90 + 360 / (teeth * 8))
                radius = outer if i % 4 in (1, 2) else inner
                point = QPointF(centre.x() + radius * math.cos(angle),
                                centre.y() + radius * math.sin(angle))
                path.moveTo(point) if i == 0 else path.lineTo(point)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawEllipse(centre, s * 0.13, s * 0.13)
        case "menu":
            painter.setPen(QPen(QColor(color), size / 12, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            for y in (0.3, 0.5, 0.7):
                painter.drawLine(QPointF(s * 0.2, s * y), QPointF(s * 0.8, s * y))
        case "voice":  # a sound wave: five bars
            painter.setPen(QPen(QColor(color), size / 9, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            for x, half in ((0.2, 0.1), (0.35, 0.24), (0.5, 0.34), (0.65, 0.18), (0.8, 0.1)):
                painter.drawLine(QPointF(s * x, s * (0.5 - half)), QPointF(s * x, s * (0.5 + half)))
        case "plus":
            painter.setPen(QPen(QColor(color), size / 11, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            painter.drawLine(QPointF(s * 0.5, s * 0.22), QPointF(s * 0.5, s * 0.78))
            painter.drawLine(QPointF(s * 0.22, s * 0.5), QPointF(s * 0.78, s * 0.5))
        case "done":  # a filled circle with the tick cut out of it
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(s / 2, s / 2), s * 0.44, s * 0.44)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(color), size / 9, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            path = QPainterPath(QPointF(s * 0.31, s * 0.52))
            path.lineTo(s * 0.44, s * 0.65)
            path.lineTo(s * 0.69, s * 0.37)
            painter.drawPath(path)
        case "copy":  # two overlapping sheets
            painter.setPen(QPen(QColor(color), size / 12))
            painter.drawRoundedRect(QRectF(s * 0.36, s * 0.36, s * 0.46, s * 0.46), 2, 2)
            path = QPainterPath(QPointF(s * 0.24, s * 0.64))
            path.lineTo(s * 0.18, s * 0.64)
            path.lineTo(s * 0.18, s * 0.18)
            path.lineTo(s * 0.64, s * 0.18)
            path.lineTo(s * 0.64, s * 0.24)
            painter.drawPath(path)
        case "folded" | "unfolded":
            if kind == "folded":  # points right
                points = ((0.4, 0.26), (0.64, 0.5), (0.4, 0.74))
            else:  # points down
                points = ((0.26, 0.4), (0.5, 0.64), (0.74, 0.4))
            path = QPainterPath(QPointF(s * points[0][0], s * points[0][1]))
            for x, y in points[1:]:
                path.lineTo(s * x, s * y)
            painter.drawPath(path)
        case "check":
            painter.setPen(QPen(QColor(color), size / 10, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            path = QPainterPath(QPointF(s * 0.22, s * 0.52))
            path.lineTo(s * 0.42, s * 0.72)
            path.lineTo(s * 0.78, s * 0.3)
            painter.drawPath(path)
        case "todo" | "dropped":  # a dashed circle
            painter.setPen(QPen(QColor(color), size / 12, Qt.PenStyle.DashLine))
            painter.drawEllipse(QPointF(s / 2, s / 2), s * 0.42, s * 0.42)
        case "minimize":
            painter.drawLine(QPointF(s * 0.25, s * 0.5), QPointF(s * 0.75, s * 0.5))
        case "maximize":
            painter.drawRoundedRect(QRectF(s * 0.27, s * 0.27, s * 0.46, s * 0.46), 1, 1)
        case "restore":
            painter.drawRoundedRect(QRectF(s * 0.25, s * 0.37, s * 0.38, s * 0.38), 1, 1)
            path = QPainterPath(QPointF(s * 0.37, s * 0.37))
            path.lineTo(s * 0.37, s * 0.25)
            path.lineTo(s * 0.75, s * 0.25)
            path.lineTo(s * 0.75, s * 0.63)
            path.lineTo(s * 0.63, s * 0.63)
            painter.drawPath(path)
        case "close":
            painter.drawLine(QPointF(s * 0.3, s * 0.3), QPointF(s * 0.7, s * 0.7))
            painter.drawLine(QPointF(s * 0.7, s * 0.3), QPointF(s * 0.3, s * 0.7))
    painter.end()
    return QIcon(pixmap)


def avatar(letter: str, size: int = 22) -> QIcon:
    """A round badge with a letter: the profile picture of someone without one."""
    c = _current
    scale = 2
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(c.accent))
    painter.drawEllipse(QRectF(0, 0, size, size))
    font = QFont(painter.font())
    font.setPixelSize(int(size * 0.5))
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    painter.setPen(QColor(c.on_accent))
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, letter[:1].upper())
    painter.end()
    return QIcon(pixmap)
