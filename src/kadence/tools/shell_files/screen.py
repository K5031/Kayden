"""Looking at the user's desktop, for the built-in Tools.

The capture is done by KDE's Spectacle, run as a child process: on Wayland a program
cannot read the screen itself, and Spectacle is the one KDE lets do it unasked. The tool
is offered only where it is installed.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
import struct
import tempfile
from pathlib import Path

from kadence.api import Image, ToolOutput, ToolSpec
from kadence.tools.shell_files.attachments import MAX_IMAGE_BYTES

NAME = "screenshot"
CAPTURE_TIMEOUT = 20.0  # seconds for the capture program
_PNG_HEADER = 24  # the signature and the start of IHDR, which holds the size

SPEC = ToolSpec(
    name=NAME,
    description=(
        "Look at the user's desktop: takes a picture of everything on their screen right "
        "now, all monitors, and shows it to you. Use it when the user asks you to look at "
        "their screen or at something on it. The picture can hold private things, so do "
        "not take one the user did not ask for. You can only look: you cannot click or type."
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    timeout=60.0,
)


class ScreenError(Exception):
    """Expected failure, reported to the model as an error result."""


class Screen:
    """The screenshot tool. `program` is the capture program taking Spectacle's
    arguments; None where there is none."""

    def __init__(self, program: str | None = None, *,
                 timeout: float = CAPTURE_TIMEOUT) -> None:
        self.program = program
        self.timeout = timeout

    @classmethod
    def found(cls) -> Screen:
        return cls(shutil.which("spectacle"))

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return (SPEC,) if self.program is not None else ()

    async def run(self) -> ToolOutput:
        if self.program is None:
            raise ScreenError("No screenshot program is installed (KDE's Spectacle).")
        folder = await asyncio.to_thread(tempfile.mkdtemp, prefix="kadence-screen-")
        try:
            raw = await self._capture(Path(folder) / "screenshot.png")
        finally:
            await asyncio.to_thread(shutil.rmtree, folder, True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ScreenError(f"The screenshot is too large to look at "
                              f"({len(raw) // 2 ** 20} MB): pictures up to "
                              f"{MAX_IMAGE_BYTES // 2 ** 20} MB can be.")
        size = ""
        if len(raw) >= _PNG_HEADER and raw.startswith(b"\x89PNG"):
            width, height = struct.unpack(">II", raw[16:_PNG_HEADER])
            size = f", {width} x {height} pixels"
        image = Image("image/png", base64.b64encode(raw).decode(), "screenshot.png")
        return ToolOutput(f"[Screenshot of the user's desktop{size}]", attachments=(image,))

    async def _capture(self, path: Path) -> bytes:
        """Background mode, no notification, the whole desktop, into `path`."""
        assert self.program is not None
        process = await asyncio.create_subprocess_exec(
            self.program, "-b", "-n", "-f", "-o", str(path),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        try:
            _, errors = await asyncio.wait_for(process.communicate(), self.timeout)
        except TimeoutError:
            raise ScreenError(f"The screenshot did not finish within {self.timeout:g} "
                              "seconds.") from None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode != 0:
            said = errors.decode(errors="replace").strip()[-500:]
            raise ScreenError(f"The screenshot failed (exit code {process.returncode})"
                              + (f": {said}" if said else "."))
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            raise ScreenError("The screenshot program made no picture.") from None
