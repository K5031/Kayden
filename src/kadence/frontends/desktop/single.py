"""One desktop window per data folder: the open app listens on a local socket, and a second
launch asks it to come forward instead of opening a window with the "already open" error.

Plain asyncio Unix sockets, so it works on any loop. The socket lives in the session's
runtime directory, named after the data folder: a path short enough for a socket name.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

SHOW = b"show\n"
CONNECT_TIMEOUT = 2.0


def socket_path(data_dir: Path) -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    key = hashlib.sha256(str(data_dir.expanduser().resolve()).encode()).hexdigest()[:16]
    return base / f"kadence-{key}.sock"


async def listen(data_dir: Path, on_show: Callable[[], object]) -> asyncio.AbstractServer:
    """Call `on_show` whenever another launch asks; replaces a socket a crash left."""

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError, asyncio.IncompleteReadError):
            if await reader.readline() == SHOW:
                on_show()
        writer.close()

    path = socket_path(data_dir)
    path.unlink(missing_ok=True)
    return await asyncio.start_unix_server(answer, path=path)


async def show_other(data_dir: Path) -> bool:
    """Whether an open desktop app on `data_dir` was asked to come forward."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path(data_dir)), CONNECT_TIMEOUT)
    except (OSError, TimeoutError):
        return False
    writer.write(SHOW)
    with contextlib.suppress(OSError):
        await writer.drain()
    writer.close()
    return True
