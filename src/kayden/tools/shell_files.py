"""Minimal Tools module: shell commands and file operations (POSIX only).

Relative paths resolve against the workspace root, which is also the shell's working
directory. Absolute paths are accepted: this is a convenience, not a sandbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import jsonschema

from kayden.api import ToolCall, ToolContext, ToolOutput, Tools, ToolSpec

SHELL_STALL_TIMEOUT = 600.0  # seconds without output before the runtime may stall-kill
TERMINATE_GRACE = 2.0  # SIGTERM -> SIGKILL delay when a command is cancelled
DRAIN_GRACE = 0.5  # how long to keep reading after the shell exits (background children)
PROGRESS_INTERVAL = 0.2  # min seconds between progress reports
MAX_OUTPUT_CHARS = 30_000  # head + tail kept from command output
MAX_READ_LINES = 2_000
MAX_READ_CHARS = 100_000
MAX_LIST_ENTRIES = 1_000
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_FILE_BYTES = 1_000_000


class ToolError(Exception):
    """Expected failure reported to the model as an is_error output."""


def _object(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_STRING = {"type": "string"}

SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="shell",
        description=(
            "Run a shell command (sh) in the workspace and return its combined stdout and "
            "stderr plus the exit code. Output is truncated to the first and last part when "
            "long. Non-zero exit codes are reported as errors."
        ),
        parameters=_object({"command": _STRING}, ["command"]),
        stall_timeout=SHELL_STALL_TIMEOUT,
    ),
    ToolSpec(
        name="read_file",
        description=(
            "Read a UTF-8 text file. Optional 1-based start line `offset` and line `limit` "
            f"(default {MAX_READ_LINES})."
        ),
        parameters=_object(
            {
                "path": _STRING,
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1},
            },
            ["path"],
        ),
        read_only=True,
    ),
    ToolSpec(
        name="write_file",
        description="Create or overwrite a text file, creating parent directories.",
        parameters=_object({"path": _STRING, "content": _STRING}, ["path", "content"]),
    ),
    ToolSpec(
        name="edit_file",
        description=(
            "Replace an exact string in a file. `old_string` must occur exactly once unless "
            "`replace_all` is true."
        ),
        parameters=_object(
            {
                "path": _STRING,
                "old_string": {"type": "string", "minLength": 1},
                "new_string": _STRING,
                "replace_all": {"type": "boolean"},
            },
            ["path", "old_string", "new_string"],
        ),
    ),
    ToolSpec(
        name="list_files",
        description=(
            "List files under a directory (default: workspace root), skipping hidden "
            "directories. `pattern` is a glob matched against the file name, or against the "
            "relative path when it contains '/'."
        ),
        parameters=_object({"path": _STRING, "pattern": _STRING}),
        read_only=True,
    ),
    ToolSpec(
        name="search_files",
        description=(
            "Search text files under a directory for a Python regular expression. Returns "
            "`path:line: text` matches. `glob` filters file names. Hidden directories, "
            "binary files and files over 1 MB are skipped."
        ),
        parameters=_object(
            {"pattern": {"type": "string", "minLength": 1}, "path": _STRING, "glob": _STRING},
            ["pattern"],
        ),
        read_only=True,
    ),
)


class ShellFilesTools(Tools):
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._specs = {spec.name: spec for spec in SPECS}
        self._handlers: dict[str, Callable[[dict[str, Any], ToolContext], Awaitable[str]]] = {
            "shell": self._shell,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "list_files": self._list_files,
            "search_files": self._search_files,
        }

    async def list_tools(self) -> Sequence[ToolSpec]:
        return SPECS

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutput:
        spec = self._specs.get(call.name)
        if spec is None:
            return ToolOutput(f"Unknown tool: {call.name}", is_error=True)
        try:
            jsonschema.validate(call.arguments, spec.parameters)
        except jsonschema.ValidationError as e:
            return ToolOutput(f"Invalid arguments: {e.message}", is_error=True)
        try:
            return ToolOutput(await self._handlers[call.name](call.arguments, ctx))
        except ToolError as e:
            return ToolOutput(str(e), is_error=True)
        except OSError as e:
            return ToolOutput(f"{type(e).__name__}: {e}", is_error=True)

    def _path(self, raw: str | None) -> Path:
        path = Path(raw or ".").expanduser()
        return path if path.is_absolute() else self.root / path

    def _display(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    # ── shell ────────────────────────────────────────────────────────────

    async def _shell(self, args: dict[str, Any], ctx: ToolContext) -> str:
        # Our own pipe rather than PIPE: asyncio's Process.wait() also waits for its pipes
        # to close, which never happens while a backgrounded child holds stdout.
        read_fd, write_fd = os.pipe()
        try:
            proc = await asyncio.create_subprocess_shell(
                args["command"],
                stdin=asyncio.subprocess.DEVNULL,
                stdout=write_fd,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.root,
                start_new_session=True,  # own process group, so cancellation reaches children
            )
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        stream = asyncio.StreamReader()
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stream), os.fdopen(read_fd, "rb", 0)
        )
        output = _OutputBuffer(MAX_OUTPUT_CHARS)
        reader = asyncio.create_task(_pump(stream, output, ctx))
        try:
            code = await proc.wait()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(reader), DRAIN_GRACE)
        except asyncio.CancelledError:
            await _terminate_group(proc)
            raise
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            transport.close()
        text = output.text()
        if code != 0:
            raise ToolError(f"{text}\n[exit code {code}]".lstrip())
        return f"{text}\n[exit code 0]".lstrip()

    # ── files ────────────────────────────────────────────────────────────

    async def _read_file(self, args: dict[str, Any], ctx: ToolContext) -> str:
        path = self._path(args["path"])
        offset = args.get("offset", 1)
        limit = args.get("limit", MAX_READ_LINES)
        if path.is_dir():
            raise ToolError(f"{self._display(path)} is a directory")

        def read() -> str:
            with path.open(encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines(keepends=True)
            selected = "".join(lines[offset - 1 : offset - 1 + limit])
            notes = []
            if len(selected) > MAX_READ_CHARS:
                selected = selected[:MAX_READ_CHARS]
                notes.append(f"[truncated at {MAX_READ_CHARS} characters]")
            if offset - 1 + limit < len(lines):
                notes.append(f"[{len(lines)} lines total; use offset to read more]")
            return "\n".join([selected, *notes]) if notes else selected

        return await asyncio.to_thread(read)

    async def _write_file(self, args: dict[str, Any], ctx: ToolContext) -> str:
        path = self._path(args["path"])
        content: str = args["content"]

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(write)
        return f"Wrote {len(content)} characters to {self._display(path)}"

    async def _edit_file(self, args: dict[str, Any], ctx: ToolContext) -> str:
        path = self._path(args["path"])
        old, new = args["old_string"], args["new_string"]
        replace_all = args.get("replace_all", False)

        def edit() -> int:
            text = path.read_text(encoding="utf-8")
            count = text.count(old)
            if count == 0:
                raise ToolError(f"old_string not found in {self._display(path)}")
            if count > 1 and not replace_all:
                raise ToolError(
                    f"old_string occurs {count} times in {self._display(path)}; "
                    "add context to make it unique or set replace_all"
                )
            path.write_text(text.replace(old, new), encoding="utf-8")
            return count

        count = await asyncio.to_thread(edit)
        return f"Replaced {count} occurrence(s) in {self._display(path)}"

    async def _list_files(self, args: dict[str, Any], ctx: ToolContext) -> str:
        base = self._path(args.get("path"))
        pattern = args.get("pattern", "*")
        if not base.is_dir():
            raise ToolError(f"{self._display(base)} is not a directory")

        def list_(cancelled: threading.Event) -> list[str]:
            found = []
            for path in _walk(base, cancelled):
                rel = path.relative_to(base).as_posix()
                if fnmatch(rel if "/" in pattern else path.name, pattern):
                    found.append(rel)
                    if len(found) > MAX_LIST_ENTRIES:
                        break
            return found

        found = await _cancellable_thread(list_)
        lines = sorted(found[:MAX_LIST_ENTRIES])
        if len(found) > MAX_LIST_ENTRIES:
            lines.append(f"[truncated at {MAX_LIST_ENTRIES} entries]")
        return "\n".join(lines) if lines else "No files found"

    async def _search_files(self, args: dict[str, Any], ctx: ToolContext) -> str:
        base = self._path(args.get("path"))
        name_glob = args.get("glob")
        try:
            regex = re.compile(args["pattern"])
        except re.error as e:
            raise ToolError(f"Invalid regular expression: {e}") from None
        if not base.is_dir():
            raise ToolError(f"{self._display(base)} is not a directory")

        def search(cancelled: threading.Event) -> list[str]:
            matches: list[str] = []
            for path in _walk(base, cancelled):
                if name_glob and not fnmatch(path.name, name_glob):
                    continue
                try:
                    if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                        continue
                    data = path.read_bytes()
                except OSError:
                    continue
                if b"\0" in data[:8192]:
                    continue
                rel = path.relative_to(base).as_posix()
                for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                    if regex.search(line):
                        matches.append(f"{rel}:{n}: {line[:500]}")
                        if len(matches) > MAX_SEARCH_MATCHES:
                            return matches
            return matches

        matches = await _cancellable_thread(search)
        lines = matches[:MAX_SEARCH_MATCHES]
        if len(matches) > MAX_SEARCH_MATCHES:
            lines.append(f"[truncated at {MAX_SEARCH_MATCHES} matches]")
        return "\n".join(lines) if lines else "No matches"


class _OutputBuffer:
    """Keeps the head and tail of a stream within a character budget."""

    def __init__(self, limit: int) -> None:
        self._half = limit // 2
        self._head = ""
        self._tail = ""
        self._dropped = 0

    def append(self, text: str) -> None:
        if len(self._head) < self._half:
            take = self._half - len(self._head)
            self._head += text[:take]
            text = text[take:]
        self._tail += text
        if len(self._tail) > self._half:
            self._dropped += len(self._tail) - self._half
            self._tail = self._tail[-self._half :]

    def text(self) -> str:
        if self._dropped:
            return f"{self._head}\n[... {self._dropped} characters omitted ...]\n{self._tail}"
        return self._head + self._tail


async def _pump(stream: asyncio.StreamReader, out: _OutputBuffer, ctx: ToolContext) -> None:
    last_report = 0.0
    pending: str | None = None
    while chunk := await stream.read(65536):
        text = chunk.decode("utf-8", "replace")
        out.append(text)
        lines = [line for line in re.split(r"[\r\n]", text) if line.strip()]
        if lines:
            pending = lines[-1][:200]
        now = time.monotonic()
        if pending is not None and now - last_report >= PROGRESS_INTERVAL:
            ctx.progress(pending)
            pending, last_report = None, now
    if pending is not None:
        ctx.progress(pending)


async def _terminate_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the command's process group, then SIGKILL whatever is left."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(proc.wait(), TERMINATE_GRACE)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(asyncio.CancelledError):
        await proc.wait()


def _walk(base: Path, cancelled: threading.Event) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(base):
        if cancelled.is_set():
            return
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            yield Path(dirpath, name)


async def _cancellable_thread[T](fn: Callable[[threading.Event], T]) -> T:
    """Run fn in a thread; on cancellation, signal it to stop instead of leaking work."""
    cancelled = threading.Event()
    try:
        return await asyncio.to_thread(fn, cancelled)
    except asyncio.CancelledError:
        cancelled.set()
        raise
