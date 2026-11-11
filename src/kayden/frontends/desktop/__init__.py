"""Desktop app frontend: PySide6 widgets on a qasync event loop.

Its factory names the event loop it runs on (loop.run_on_qt: a QApplication and qasync's
loop); `run` needs both. PySide6 and qasync are imported here, so the entry point loads (and
the app is offered in settings) only where the `desktop` extra is installed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from pathlib import Path
from typing import Any

from kayden.api import Frontend, FrontendInfo, InstancePaths, RuntimeAPI

# PySide6 6.12 on Python 3.15 breaks reading a lazily imported module attribute (PEP 810)
# from another module once Qt is loaded. asyncio.to_thread reads this one, so resolve it
# before any Qt import.
_ = concurrent.futures.ThreadPoolExecutor

import qasync  # noqa: F401
from PySide6 import QtWidgets  # noqa: F401

from kayden.frontends.desktop import single
from kayden.frontends.desktop.loop import run_on_qt


def factory(options: dict[str, Any], paths: InstancePaths) -> DesktopFrontend:
    """Options: conversation_id (open it first; default: the newest)."""
    return DesktopFrontend(options.get("conversation_id"), paths.data_dir)


ZOOM_FILE = "desktop.json"  # the app's own view preferences, in the data folder


factory.display_name = "Desktop app"  # type: ignore[attr-defined]
factory.kind = "window"  # type: ignore[attr-defined]
factory.event_loop = run_on_qt  # type: ignore[attr-defined]


class DesktopFrontend(Frontend):
    def __init__(self, conversation_id: str | None = None,
                 data_dir: Path | None = None) -> None:
        self.conversation_id = conversation_id
        self.data_dir = data_dir  # None: no zoom remembered, no single window
        self.zoom_file = data_dir / ZOOM_FILE if data_dir is not None else None

    @property
    def info(self) -> FrontendInfo:
        return FrontendInfo(name="desktop", interactive_permissions=True, kind="window")

    async def run(self, runtime: RuntimeAPI) -> None:
        """Returns when the main window closes or the runtime's event stream ends."""
        from PySide6.QtWidgets import QApplication

        from kayden.frontends.desktop import theme
        from kayden.frontends.desktop.viewmodel import Controller, ConversationModel
        from kayden.frontends.desktop.window import MainWindow, load_zoom

        app = QApplication.instance()
        if app is None:
            raise RuntimeError("the desktop frontend runs on its own event loop (factory.event_loop)")
        if self.data_dir is not None and (await runtime.settings()).already_open:
            await self._hand_over(self.data_dir)
            return
        if self.zoom_file is not None:
            theme.set_zoom(app, await load_zoom(self.zoom_file))
        controller = Controller(runtime, ConversationModel())
        window = MainWindow(runtime, controller, self.zoom_file)
        server = (await single.listen(self.data_dir, window.bring_forward)
                  if self.data_dir is not None else None)
        closed = asyncio.Event()
        window.closed.connect(closed.set)
        window.show()
        events = asyncio.create_task(controller.run())
        controller.command(window.begin(self.conversation_id))
        waiter = asyncio.create_task(closed.wait())
        try:
            await asyncio.wait({events, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (events, waiter):
                task.cancel()
            if server is not None:
                server.close()
                single.socket_path(self.data_dir).unlink(missing_ok=True)
            window.close()
            await window.save_pending_zoom()

    async def _hand_over(self, data_dir: Path) -> None:
        """Another Kayden has the data folder: bring its window forward, or, when none
        answers (a terminal has it), say so once."""
        from PySide6.QtWidgets import QMessageBox

        if await single.show_other(data_dir):
            return
        box = QMessageBox(QMessageBox.Icon.Information, "Kayden",
                          "Kayden is already open, probably in a terminal. Quit it there, "
                          "then open Kayden again.")
        closed = asyncio.get_running_loop().create_future()
        box.finished.connect(lambda _: closed.done() or closed.set_result(None))
        box.open()
        await closed
