"""Desktop app frontend: PySide6 widgets on a qasync event loop.

Run it with `kayden-desktop` (kayden.frontends.desktop.launcher), which creates the
QApplication and runs the instance on qasync's loop; `run` needs both.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

from kayden.api import Frontend, FrontendInfo, InstancePaths, RuntimeAPI

# PySide6 6.12 on Python 3.15 breaks reading a lazily imported module attribute (PEP 810)
# from another module once Qt is loaded. asyncio.to_thread reads this one, so resolve it
# before any Qt import.
_ = concurrent.futures.ThreadPoolExecutor


def factory(options: dict[str, Any], paths: InstancePaths) -> DesktopFrontend:
    return DesktopFrontend()


class DesktopFrontend(Frontend):
    @property
    def info(self) -> FrontendInfo:
        return FrontendInfo(name="desktop", interactive_permissions=True)

    async def run(self, runtime: RuntimeAPI) -> None:
        """Returns when the main window closes or the runtime's event stream ends."""
        from PySide6.QtWidgets import QApplication

        from kayden.frontends.desktop.viewmodel import Controller, ConversationModel
        from kayden.frontends.desktop.window import MainWindow

        if QApplication.instance() is None:
            raise RuntimeError("the desktop frontend runs through kayden-desktop")
        controller = Controller(runtime, ConversationModel())
        window = MainWindow(runtime, controller)
        closed = asyncio.Event()
        window.closed.connect(closed.set)
        window.show()
        events = asyncio.create_task(controller.run())
        controller.command(window.begin())
        waiter = asyncio.create_task(closed.wait())
        try:
            await asyncio.wait({events, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (events, waiter):
                task.cancel()
            window.close()
