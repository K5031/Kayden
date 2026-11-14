"""A second desktop Kadence on the same data folder brings the open window forward and exits,
instead of opening a window of its own with the "already open" error."""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from typing import Any

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from kadence.frontends import desktop
from kadence.frontends.desktop import single
from kadence.runtime.config import RuntimeConfig
from kadence.runtime.setup import SetupRuntime
from tests.conftest import eventually


@pytest.fixture
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)  # the launcher's setting: the runtime ends the loop
    return app


async def test_a_second_launch_asks_the_first_to_come_forward(tmp_path):
    shown = []
    server = await single.listen(tmp_path, lambda: shown.append(True))
    try:
        assert await single.show_other(tmp_path)
        await eventually(lambda: shown)
    finally:
        server.close()
        await server.wait_closed()


async def test_nobody_answers_when_no_window_listens(tmp_path):
    assert not await single.show_other(tmp_path)
    single.socket_path(tmp_path).write_text("left by a crash")
    server = await single.listen(tmp_path, lambda: None)  # a stale socket is replaced
    server.close()
    await server.wait_closed()


def already_open(tmp_path: Any) -> SetupRuntime:
    return SetupRuntime(RuntimeConfig(data_dir=tmp_path), "window",
                        "Kadence is already open in another window (in use)", already_open=True)


async def test_the_second_desktop_app_hands_over_and_opens_nothing(tmp_path, qapp, monkeypatch):
    shown = []
    server = await single.listen(tmp_path, lambda: shown.append(True))
    monkeypatch.setattr("kadence.frontends.desktop.window.MainWindow",
                        lambda *a, **k: pytest.fail("no window of its own"))
    try:
        await asyncio.wait_for(desktop.DesktopFrontend(data_dir=tmp_path).run(
            already_open(tmp_path)), 5)
        await eventually(lambda: shown)
    finally:
        server.close()
        await server.wait_closed()


async def test_with_no_window_to_bring_forward_one_message_says_why(tmp_path, qapp):
    run = asyncio.ensure_future(desktop.DesktopFrontend(data_dir=tmp_path).run(
        already_open(tmp_path)))
    await eventually(lambda: [b for b in qapp.topLevelWidgets()
                              if isinstance(b, QMessageBox) and b.isVisible()])
    [box] = [b for b in qapp.topLevelWidgets() if isinstance(b, QMessageBox) and b.isVisible()]
    assert "terminal" in box.text()
    box.buttons()[0].click()
    await asyncio.wait_for(run, 5)
