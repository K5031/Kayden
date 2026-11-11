"""The event loop the desktop app runs on (its factory's event_loop): a QApplication and
qasync's QEventLoop, since a GUI toolkit's loop must be the one that runs. A failure to
start is shown in a dialog: a double-clicked app has no visible stderr."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import sys
from collections.abc import Coroutine
from typing import Any

import qasync

from kayden.runtime import ModuleLoadError

GC_CHECK_S = 0.1


class QtEventLoop(qasync.QEventLoop):
    """qasync's loop with three fixes.

    Cyclic garbage is collected on the Qt thread only. Python collects in whichever thread
    allocates, such as an asyncio.to_thread worker importing a module, and a QObject destroyed
    off its own thread leaves its timers registered: the next one to fire crashes the process.
    While the loop is open, automatic collection is off and the loop collects by the same
    thresholds.

    run_until_complete does not process Qt events once the loop has stopped. qasync's does, and
    a task woken there fails ("not the running loop") and never resumes; asyncio.run calls it
    several times while shutting down. Callbacks still due run in the next run, as in asyncio.

    _add_reader and _add_writer take the `context` Python 3.15's transports pass, and run the
    callback in it; qasync's take none, so every socket read failed.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._gc_was_enabled = gc.isenabled()
        gc.disable()
        self._gc_check: asyncio.TimerHandle | None = self.call_later(GC_CHECK_S, self._collect)

    def _collect(self) -> None:
        due = [g for g, (count, threshold) in enumerate(zip(gc.get_count(), gc.get_threshold()))
               if threshold and count > threshold]
        if due:
            gc.collect(due[-1])
        self._gc_check = self.call_later(GC_CHECK_S, self._collect)

    def run_until_complete(self, future: Any) -> Any:
        if self.is_running():
            raise RuntimeError("Event loop already running")
        future = asyncio.ensure_future(future, loop=self)

        def stop(_: Any) -> None:
            self.stop()

        future.add_done_callback(stop)
        try:
            self.run_forever()
        finally:
            future.remove_done_callback(stop)
        if not future.done():
            raise RuntimeError("Event loop stopped before Future completed.")
        return future.result()

    def _add_reader(self, fd: Any, callback: Any, *args: Any,
                    context: contextvars.Context | None = None) -> None:
        if context is not None:
            callback, args = context.run, (callback, *args)
        super()._add_reader(fd, callback, *args)

    def _add_writer(self, fd: Any, callback: Any, *args: Any,
                    context: contextvars.Context | None = None) -> None:
        if context is not None:
            callback, args = context.run, (callback, *args)
        super()._add_writer(fd, callback, *args)

    def close(self) -> None:
        super().close()
        if self._gc_check is not None:
            self._gc_check.cancel()
            self._gc_check = None
            if self._gc_was_enabled:
                gc.enable()


def run_on_qt(main: Coroutine[Any, Any, Any]) -> Any:
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
        return asyncio.run(main, loop_factory=QtEventLoop)
    except SystemExit as e:  # a bad config file
        if isinstance(e.code, str):
            QMessageBox.critical(None, "Kayden", f"Kayden could not start: {e.code}")
        raise
    except ModuleLoadError as e:
        QMessageBox.critical(None, "Kayden", f"Kayden could not start: {e}")
        raise SystemExit(1) from None
