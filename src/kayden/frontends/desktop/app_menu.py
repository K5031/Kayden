"""A launcher for Kayden in the Linux app menu, for an install that is not a bundle.

A desktop entry and an icon in the user's XDG data directory, which desktops pick up by
themselves. The entry starts the desktop launcher with this install's Python: a menu has
no terminal for `kayden` to run a terminal frontend in.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_ID = "io.github.k5031.kayden"  # also the window's desktop file name, so they match
ICON = Path(__file__).parent / "icons" / "kayden.svg"
_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def supported() -> bool:
    return sys.platform == "linux"


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def _entry_path() -> Path:
    return _data_home() / "applications" / f"{APP_ID}.desktop"


def _icon_path() -> Path:
    return _data_home() / "icons/hicolor/scalable/apps" / f"{APP_ID}.svg"


def _quoted(argument: str) -> str:
    """An Exec argument as the Desktop Entry Specification wants it."""
    if not _RESERVED & set(argument):
        return argument
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in argument)
    return f'"{escaped}"'


def _entry() -> str:
    return "\n".join((
        "[Desktop Entry]",
        "Type=Application",
        "Name=Kayden",
        "Comment=An AI agent that works for you on your computer",
        f"Exec={_quoted(sys.executable)} -m kayden.frontends.desktop.launcher",
        f"Icon={APP_ID}",
        "Terminal=false",
        "Categories=Utility;",
        f"StartupWMClass={APP_ID}",
        "",
    ))


def is_current() -> bool:
    """Whether the app menu already starts this install."""
    try:
        return (_entry_path().read_text() == _entry()
                and _icon_path().read_bytes() == ICON.read_bytes())
    except OSError:
        return False


def install() -> None:
    for path in (_entry_path(), _icon_path()):
        path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON, _icon_path())
    _entry_path().write_text(_entry())
