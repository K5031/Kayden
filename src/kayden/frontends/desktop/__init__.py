"""Desktop app frontend: PySide6 widgets on a qasync event loop.

It runs through kayden.frontends.desktop.launcher, which creates the
QApplication and runs the instance on qasync's loop; `run` needs both. PySide6 and qasync
are imported here, so the entry point loads (and the app is offered in settings) only where
the `desktop` extra is installed.
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


def factory(options: dict[str, Any], paths: InstancePaths) -> DesktopFrontend:
    """Options: conversation_id (open it first; default: the newest)."""
    return DesktopFrontend(options.get("conversation_id"), paths.data_dir / ZOOM_FILE)


ZOOM_FILE = "desktop.json"  # the app's own view preferences, in the data folder


factory.display_name = "Desktop app"  # type: ignore[attr-defined]
factory.kind = "window"  # type: ignore[attr-defined]


class DesktopFrontend(Frontend):
    def __init__(self, conversation_id: str | None = None,
                 zoom_file: Path | None = None) -> None:
        self.conversation_id = conversation_id
        self.zoom_file = zoom_file

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
            raise RuntimeError("the desktop frontend runs through kayden.frontends.desktop.launcher")
        if self.zoom_file is not None:
            theme.set_zoom(app, await load_zoom(self.zoom_file))
        controller = Controller(runtime, ConversationModel())
        window = MainWindow(runtime, controller, self.zoom_file)
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
            window.close()
            await window.save_pending_zoom()
