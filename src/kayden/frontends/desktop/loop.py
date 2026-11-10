"""The event loop the desktop app runs on (its factory's event_loop): a QApplication and
qasync's QEventLoop, since a GUI toolkit's loop must be the one that runs. A failure to
start is shown in a dialog: a double-clicked app has no visible stderr."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any

from kayden.runtime import ModuleLoadError


def run_on_qt(main: Coroutine[Any, Any, Any]) -> Any:
    import qasync
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from kayden.frontends.desktop import app_menu, theme

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication(sys.argv[:1])
        app.setApplicationName("Kayden")
        app.setDesktopFileName(app_menu.APP_ID)
        app.setWindowIcon(QIcon(str(app_menu.ICON)))
        app.setQuitOnLastWindowClosed(False)  # the runtime shuts down first, then the loop
        theme.apply(app)
        theme.follow_system(app)
    try:
        return asyncio.run(main, loop_factory=qasync.QEventLoop)
    except SystemExit as e:  # a bad config file
        if isinstance(e.code, str):
            QMessageBox.critical(None, "Kayden", f"Kayden could not start: {e.code}")
        raise
    except ModuleLoadError as e:
        QMessageBox.critical(None, "Kayden", f"Kayden could not start: {e}")
        raise SystemExit(1) from None
