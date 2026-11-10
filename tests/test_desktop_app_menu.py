"""The launcher entry the desktop app adds to the Linux app menu."""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

import pytest

from kayden.frontends.desktop import app_menu


@pytest.fixture
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def entry(data_home: Path) -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(data_home / "applications" / "io.github.k5031.kayden.desktop")
    return parser["Desktop Entry"]


def test_install_adds_a_launcher_that_starts_the_desktop_app(data_home: Path) -> None:
    assert app_menu.supported() and not app_menu.is_current()
    app_menu.install()
    e = entry(data_home)
    assert (e["Type"], e["Name"], e["Terminal"]) == ("Application", "Kayden", "false")
    # The desktop launcher, not `kayden`: a menu has no terminal for the terminal app.
    assert e["Exec"] == f"{sys.executable} -m kayden.frontends.desktop.launcher"
    assert e["Icon"] == e["StartupWMClass"] == "io.github.k5031.kayden"
    icon = data_home / "icons/hicolor/scalable/apps/io.github.k5031.kayden.svg"
    assert icon.read_text() == app_menu.ICON.read_text() and "<svg" in icon.read_text()
    assert app_menu.is_current()


def test_an_entry_for_another_install_is_not_current(data_home: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    app_menu.install()
    monkeypatch.setattr(sys, "executable", "/opt/my tools/kayden/bin/python")
    assert not app_menu.is_current()
    app_menu.install()
    assert entry(data_home)["Exec"] == (
        '"/opt/my tools/kayden/bin/python" -m kayden.frontends.desktop.launcher')
    assert app_menu.is_current()


def test_only_linux_has_an_app_menu_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert not app_menu.supported()


def test_an_older_icon_is_not_current(data_home: Path) -> None:
    app_menu.install()
    icon = data_home / "icons/hicolor/scalable/apps/io.github.k5031.kayden.svg"
    icon.write_text("<svg>old</svg>")
    assert not app_menu.is_current()  # Add to App Menu shows again, to update it
