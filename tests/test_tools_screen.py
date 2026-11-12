"""The screenshot tool, against a stand-in for Spectacle."""

import asyncio
import base64
import struct
import zlib
from pathlib import Path

import pytest

from kayden.api import Image, ToolCall
from kayden.tools.shell_files import ShellFilesTools, screen
from tests.conformance.tools_suite import RecordingContext


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))

    rows = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8,
                                                              2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def program(tmp_path: Path, script: str) -> str:
    """A program taking Spectacle's arguments: `$out` is the file it was told to write."""
    path = tmp_path / "spectacle"
    path.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
                    + script + "\n")
    path.chmod(0o755)
    return str(path)


def tools(tmp_path: Path, script: str | None, **kw: float) -> ShellFilesTools:
    return ShellFilesTools(tmp_path / "ws", screen_tools=screen.Screen(
        None if script is None else program(tmp_path, script), **kw))


async def shoot(module: ShellFilesTools):
    return await module.execute(ToolCall("c", "screenshot", {}), RecordingContext("c"))


async def test_the_screen_is_returned_as_a_picture(tmp_path: Path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(png(64, 36))
    out = await shoot(tools(tmp_path, f'cp {shot} "$out"'))
    assert not out.is_error and "64 x 36" in out.content
    assert out.attachments == (
        Image("image/png", base64.b64encode(shot.read_bytes()).decode(), "screenshot.png"),)


async def test_it_asks_like_any_tool_that_reaches_outside_the_folder(tmp_path: Path):
    module = tools(tmp_path, "true")
    [spec] = [s for s in await module.list_tools() if s.name == "screenshot"]
    assert not spec.read_only
    assert not await module.confined(ToolCall("c", "screenshot", {}))


async def test_it_is_not_offered_without_a_capture_program(tmp_path: Path):
    module = tools(tmp_path, None)
    assert "screenshot" not in {s.name for s in await module.list_tools()}
    assert (await shoot(module)).is_error


@pytest.mark.parametrize("script, says", [
    ("echo no display >&2; exit 1", "no display"),
    ("true", "no picture"),
    ("sleep 30", "did not finish"),
])
async def test_a_capture_that_fails_is_an_error(tmp_path: Path, script: str, says: str):
    out = await shoot(tools(tmp_path, script, timeout=0.3))
    assert out.is_error and says in out.content and not out.attachments


async def test_a_picture_too_large_for_a_provider_is_an_error(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(screen, "MAX_IMAGE_BYTES", 10)
    shot = tmp_path / "shot.png"
    shot.write_bytes(png(64, 36))
    out = await shoot(tools(tmp_path, f'cp {shot} "$out"'))
    assert out.is_error and "too large" in out.content


async def test_stop_ends_the_capture(tmp_path: Path):
    marker = tmp_path / "pid"
    module = tools(tmp_path, f"echo $$ > {marker}; exec sleep 30")
    task = asyncio.create_task(shoot(module))
    while not marker.exists() or not marker.read_text().strip():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not Path(f"/proc/{marker.read_text().strip()}").exists()
