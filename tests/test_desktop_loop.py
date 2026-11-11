"""The desktop app's event loop."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio
import gc
import threading
import weakref
from typing import Any

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from kayden.frontends.desktop.loop import QtEventLoop
from tests.conftest import eventually


def test_garbage_qt_objects_are_freed_on_the_qt_thread() -> None:
    """Qt objects must be destroyed on their own thread. Python's cyclic garbage collector
    runs in whichever thread allocates, such as a worker importing a module, and freeing a
    QObject there leaves its timers registered: the app crashes when one fires."""
    app = QApplication.instance() or QApplication([])
    freed_in: list[str] = []

    async def main() -> None:
        obj = QObject()
        obj.cycle = obj  # type: ignore[attr-defined]
        weakref.finalize(obj, lambda: freed_in.append(threading.current_thread().name))
        obj.startTimer(60_000)
        holder = [obj]
        del obj

        def work() -> list[list[Any]]:
            holder.clear()  # the QObject is now garbage, in a cycle
            return [[] for _ in range(300_000)]  # enough to trigger every generation

        await asyncio.to_thread(work)
        await eventually(lambda: freed_in)

    gc.collect()
    asyncio.run(main(), loop_factory=lambda: QtEventLoop(app))
    assert freed_in == [threading.main_thread().name]
    assert gc.isenabled()  # restored when the loop closes


def test_a_task_woken_as_a_run_ends_resumes_in_the_next_run() -> None:
    """Callbacks due when run_until_complete returns wait for the next run: run outside a
    running loop, a task's wakeup fails and the task never resumes."""
    app = QApplication.instance() or QApplication([])
    loop = QtEventLoop(app)

    async def background() -> str:
        await asyncio.sleep(0)
        return "done"

    async def first() -> asyncio.Task[str]:
        return asyncio.create_task(background())

    try:
        task = loop.run_until_complete(first())
        loop.run_until_complete(asyncio.wait([task], timeout=2))  # wait_for would hang too
        assert task.done() and task.result() == "done"
    finally:
        loop.close()


def test_streams_work(tmp_path: Any) -> None:
    """Python 3.15's transports pass `context` to the loop's _add_reader and _add_writer,
    which qasync's do not take: every socket read failed."""
    app = QApplication.instance() or QApplication([])

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.readline())
        await writer.drain()
        writer.close()

    async def main() -> bytes:
        async with asyncio.timeout(2):
            server = await asyncio.start_unix_server(echo, path=tmp_path / "s.sock")
            async with server:
                reader, writer = await asyncio.open_unix_connection(tmp_path / "s.sock")
                writer.write(b"hello\n")
                reply = await reader.readline()
                writer.close()
                return reply

    assert asyncio.run(main(), loop_factory=lambda: QtEventLoop(app)) == b"hello\n"
