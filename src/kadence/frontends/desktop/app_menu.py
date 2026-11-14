"""A launcher for Kadence in the Linux app menu, for an install that is not a bundle.

A desktop entry and an icon in the user's XDG data directory, which desktops pick up by
themselves. The entry starts the window launcher with this install's Python: a menu has
no terminal for `kadence` to run a terminal frontend in.

The icon's name carries a hash of its content: desktops cache icons by name (KDE keeps
showing a replaced file), so a new icon gets a new name and older ones are removed. KDE's
running programs (the app launcher) also keep the icons they looked up, so installing
tells them to reload, as `kiconloader` does after an icon change.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "io.github.k5031.kadence"  # also the window's desktop file name, so they match
ICON = Path(__file__).parent / "icons" / "kadence.svg"
ICON_NAME = f"{APP_ID}-{hashlib.sha256(ICON.read_bytes()).hexdigest()[:8]}"
_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def supported() -> bool:
    return sys.platform == "linux"


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def _entry_path() -> Path:
    return _data_home() / "applications" / f"{APP_ID}.desktop"


def _icon_path() -> Path:
    return _data_home() / "icons/hicolor/scalable/apps" / f"{ICON_NAME}.svg"


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
        "Name=Kadence",
        "Comment=An AI agent that works for you on your computer",
        f"Exec={_quoted(sys.executable)} -m kadence.window",
        f"Icon={ICON_NAME}",
        "Terminal=false",
        "Categories=Utility;",
        f"StartupWMClass={APP_ID}",
        "",
    ))


def is_current() -> bool:
    """Whether the app menu already starts this install."""
    try:
        return _entry_path().read_text() == _entry() and _icon_path().exists()
    except OSError:
        return False


def install() -> None:
    for path in (_entry_path(), _icon_path()):
        path.parent.mkdir(parents=True, exist_ok=True)
    for old in _icon_path().parent.glob(f"{APP_ID}*.svg"):
        if old != _icon_path():
            old.unlink()
    shutil.copyfile(ICON, _icon_path())
    _entry_path().write_text(_entry())
    _reload_icons()


def _reload_icons() -> None:
    """Best effort, for KDE, which keeps the entries and icons it has read: rebuild its menu
    cache (else a click runs the entry's old command) and signal that icons changed. Other
    desktops have neither program or ignore the signal."""
    commands = [["kbuildsycoca6"], ["dbus-send", "--session", "--type=signal", "/KIconLoader",
                                    "org.kde.KIconLoader.iconChanged", "int32:0"]]
    for name, *rest in commands:
        if (program := shutil.which(name)) is None:
            continue
        try:
            subprocess.run([program, *rest], check=False, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
