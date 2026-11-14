"""The launcher entry the desktop app adds to the Linux app menu."""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

import pytest

from kadence.frontends.desktop import app_menu


@pytest.fixture
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(app_menu, "_reload_icons", lambda: None)  # not the real session
    return tmp_path


def entry(data_home: Path) -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(data_home / "applications" / "io.github.k5031.kadence.desktop")
    return parser["Desktop Entry"]


def test_install_adds_a_launcher_that_starts_the_desktop_app(data_home: Path) -> None:
    assert app_menu.supported() and not app_menu.is_current()
    app_menu.install()
    e = entry(data_home)
    assert (e["Type"], e["Name"], e["Terminal"]) == ("Application", "Kadence", "false")
    # The window launcher, not `kadence`: a menu has no terminal for the terminal app.
    assert e["Exec"] == f"{sys.executable} -m kadence.window"
    assert e["StartupWMClass"] == "io.github.k5031.kadence"
    assert e["Icon"] == app_menu.ICON_NAME and e["Icon"].startswith("io.github.k5031.kadence-")
    icon = data_home / f"icons/hicolor/scalable/apps/{app_menu.ICON_NAME}.svg"
    assert icon.read_text() == app_menu.ICON.read_text() and "<svg" in icon.read_text()
    assert app_menu.is_current()


def test_an_entry_for_another_install_is_not_current(data_home: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    app_menu.install()
    monkeypatch.setattr(sys, "executable", "/opt/my tools/kadence/bin/python")
    assert not app_menu.is_current()
    app_menu.install()
    assert entry(data_home)["Exec"] == (
        '"/opt/my tools/kadence/bin/python" -m kadence.window')
    assert app_menu.is_current()


def test_only_linux_has_an_app_menu_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert not app_menu.supported()


def test_a_new_icon_gets_a_new_name_and_replaces_the_old_one(data_home: Path) -> None:
    """Desktops cache icons by name, so a changed icon must not keep its name."""
    apps = data_home / "icons/hicolor/scalable/apps"
    apps.mkdir(parents=True)
    (apps / "io.github.k5031.kadence.svg").write_text("<svg>old</svg>")
    (apps / "io.github.k5031.kadence-0ld0ld00.svg").write_text("<svg>older</svg>")
    (data_home / "applications").mkdir()
    (data_home / "applications/io.github.k5031.kadence.desktop").write_text(
        "[Desktop Entry]\nIcon=io.github.k5031.kadence\n")
    (apps / "other-app.svg").write_text("<svg/>")
    assert not app_menu.is_current()  # Add to App Menu shows again, to update it
    app_menu.install()
    assert sorted(p.name for p in apps.iterdir()) == [f"{app_menu.ICON_NAME}.svg",
                                                      "other-app.svg"]
    assert app_menu.is_current()


def test_install_tells_running_programs_to_reload_icons(data_home: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """KDE keeps menu entries and icons it has read until it is told they changed: its
    menu cache is rebuilt, or a click still runs the old command."""
    ran: list[list[str]] = []
    monkeypatch.undo()  # the real _reload_icons, with which and run replaced below
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setattr(app_menu.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(app_menu.subprocess, "run", lambda args, **kw: ran.append(args))
    app_menu.install()
    assert ran == [["/usr/bin/kbuildsycoca6"],
                   ["/usr/bin/dbus-send", "--session", "--type=signal", "/KIconLoader",
                    "org.kde.KIconLoader.iconChanged", "int32:0"]]


def test_install_works_without_dbus_send(data_home: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_menu.shutil, "which", lambda name: None)
    app_menu.install()
    assert app_menu.is_current()
