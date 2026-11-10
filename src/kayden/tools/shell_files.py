"""The built-in Tools module: shell commands, file operations and the web (POSIX only).

Relative paths resolve against the workspace root, which is also the shell's working
directory. Commands inherit the process environment (PERMISSION_CEILING_ENV included);
sandboxed ones without the variables that look like secrets (sandbox_environment).

The workspace is a boundary the runtime can rely on (`confined`): a file call is confined
when its path is inside it, and a shell command when it runs in the bubblewrap sandbox,
where only the workspace is writable and the home directories are hidden. The web tools
(`web.py`) touch nothing on this computer, so they are confined too. Calls that reach
outside still work; the runtime asks the user first.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import httpx2
import jsonschema

from kayden.api import InstancePaths, ToolCall, ToolContext, ToolOutput, Tools, ToolSpec
from kayden.tools import web

SHELL_STALL_TIMEOUT = 600.0  # seconds without output before the runtime may stall-kill
HEARTBEAT_INTERVAL = 0.5  # min seconds between silent heartbeats from file walks
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


# Set while a call that `confined` allowed runs: its files must really be in the workspace,
# wherever a link that appeared since the check leads.
_must_stay_inside = contextvars.ContextVar("must_stay_inside", default=False)


def _object(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_STRING = {"type": "string"}

SHELL = ToolSpec(
    name="shell",
    description=(
        "Run a shell command (sh) in the workspace and return its combined stdout and "
        "stderr plus the exit code. Output is truncated to the first and last part when "
        "long. Non-zero exit codes are reported as errors."
    ),
    parameters=_object({"command": _STRING, "unsandboxed": {"type": "boolean"}}, ["command"]),
    stall_timeout=SHELL_STALL_TIMEOUT,
)
SANDBOX_NOTE = (
    " Commands run in a sandbox: they can read and write the workspace, read the system "
    "and use the network, but the rest of the home directory and environment variables "
    "holding keys or tokens are hidden, and nothing outside the workspace can be "
    "changed. Set `unsandboxed` to true only when a command needs something outside; "
    "the user is then asked first, so say why."
)
# Variables whose names look like secrets (API keys, tokens, passwords) are left out of a
# sandboxed command's environment: it runs unasked and may use the network.
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL", re.IGNORECASE)
SANDBOX_CHECK_TIMEOUT = 5.0  # seconds for the one test run of the sandbox at startup
FILE_SPECS: tuple[ToolSpec, ...] = (
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


def factory(options: dict[str, Any], paths: InstancePaths) -> ShellFilesTools:
    """Options: root (default: the current directory), shell_timeout, shell_stall_timeout."""
    options = dict(options)
    return ShellFilesTools(Path(options.pop("root", Path.cwd())), **options)


factory.display_name = "Shell, files and web"  # type: ignore[attr-defined]


def sandbox_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in environ.items() if not SECRET_NAME.search(k)}


def _sandbox(bwrap: str, root: Path, home: Path) -> list[str]:
    """The bubblewrap command line that confines a shell command to `root`.

    The whole system is visible read-only, so installed programs run; home directories,
    /tmp and the session's runtime directory are replaced by empty ones, and only the
    workspace is bound back, writable. The network is left as it is."""
    hidden = [p for p in (Path("/home"), Path("/root"), Path("/run/user"), Path("/tmp"), home)
              if p.is_dir()]
    command = [bwrap, "--ro-bind", "/", "/"]
    for path in hidden:
        command += ["--tmpfs", str(path)]
    return command + ["--dir", str(home), "--bind", str(root), str(root), "--dev", "/dev",
                      "--proc", "/proc", "--unshare-pid", "--die-with-parent",
                      "--chdir", str(root)]


def _bwrap(root: Path, home: Path) -> str | None:
    """bubblewrap, if a sandbox works here (not when it is missing, user namespaces are
    disabled, or Kayden itself runs in a sandbox). Blocking: run it where module
    factories run, off the event loop."""
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return None
    try:
        works = subprocess.run([*_sandbox(bwrap, root, home), "true"], check=False,
                               capture_output=True, timeout=SANDBOX_CHECK_TIMEOUT
                               ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        works = False
    return bwrap if works else None


class ShellFilesTools(Tools):
    """`shell_timeout`: hard limit per command (None = runtime default, math.inf = none).
    `shell_stall_timeout`: seconds without output before the command counts as hung."""

    def __init__(
        self,
        root: Path,
        *,
        shell_timeout: float | None = None,
        shell_stall_timeout: float | None = SHELL_STALL_TIMEOUT,
        web_tools: web.Web | None = None,
    ) -> None:
        self._default = Path(root).expanduser().resolve()
        self._bwrap = _bwrap(self._default, Path.home().resolve())
        self._shell_spec = replace(SHELL, timeout=shell_timeout,
                                   stall_timeout=shell_stall_timeout)
        self._web = web_tools or web.Web()
        self._confined_calls: set[str] = set()  # file calls `confined` allowed, by call id
        self._set_root(self._default)
        self._handlers: dict[str, Callable[[dict[str, Any], ToolContext], Awaitable[str]]] = {
            "shell": self._shell,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "list_files": self._list_files,
            "search_files": self._search_files,
            **{name: self._web_call(name) for name in web.NAMES},
        }

    def _set_root(self, root: Path) -> None:
        self.root = root
        home = Path.home().resolve()
        # A workspace that is or holds the home directory (or is /) is no boundary.
        self._bounded = root != home and root not in home.parents
        self._sandbox = (_sandbox(self._bwrap, root, home)
                         if self._bwrap is not None and self._bounded else None)
        shell = self._shell_spec
        if self._sandbox is not None:
            shell = replace(shell, description=shell.description + SANDBOX_NOTE)
        self._spec_list = (shell, *FILE_SPECS, *web.SPECS)
        self._specs = {spec.name: spec for spec in self._spec_list}

    async def list_tools(self) -> Sequence[ToolSpec]:
        return self._spec_list

    async def use_folder(self, folder: Path | None) -> None:
        root = self._default if folder is None else Path(folder).expanduser().resolve()
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        self._set_root(root)

    async def confined(self, call: ToolCall) -> bool:
        if call.name in web.NAMES:
            return True
        if not self._bounded or call.name not in self._handlers:
            return False
        if call.name == "shell":
            return self._sandbox is not None and call.arguments.get("unsandboxed") is not True
        path = call.arguments.get("path", ".")
        inside = isinstance(path, str) and await asyncio.to_thread(self._inside, path)
        if inside:
            self._confined_calls.add(call.id)
        return inside

    def _inside(self, raw: str) -> bool:
        """Whether the path, with symlinks followed, is in the workspace."""
        return self._path(raw).resolve().is_relative_to(self.root)

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutput:
        spec = self._specs.get(call.name)
        if spec is None:
            return ToolOutput(f"Unknown tool: {call.name}", is_error=True)
        try:
            jsonschema.validate(call.arguments, spec.parameters)
        except jsonschema.ValidationError as e:
            return ToolOutput(f"Invalid arguments: {e.message}", is_error=True)
        token = _must_stay_inside.set(call.id in self._confined_calls)
        self._confined_calls.discard(call.id)
        try:
            return ToolOutput(await self._handlers[call.name](call.arguments, ctx))
        except (ToolError, web.WebError) as e:
            return ToolOutput(str(e), is_error=True)
        except httpx2.HTTPError as e:
            return ToolOutput(f"The page could not be read: {type(e).__name__}: {e}",
                              is_error=True)
        except OSError as e:
            return ToolOutput(f"{type(e).__name__}: {e}", is_error=True)
        finally:
            _must_stay_inside.reset(token)

    def _web_call(self, name: str) -> Callable[[dict[str, Any], ToolContext], Awaitable[str]]:
        async def call(args: dict[str, Any], ctx: ToolContext) -> str:
            return await self._web.run(name, args)

        return call

    def _path(self, raw: str | None) -> Path:
        path = Path(raw or ".").expanduser()
        return path if path.is_absolute() else self.root / path

    def _open(self, path: Path, flags: int) -> int:
        """os.open, for a call `confined` allowed only if the file opened is in the
        workspace: checked on the open file, so a link swapped in after the check (or
        between this check and the read or write) cannot lead outside."""
        if not _must_stay_inside.get():
            return os.open(path, flags, 0o644)
        fd = os.open(path.resolve(), flags | os.O_NOFOLLOW, 0o644)
        if not Path(os.readlink(f"/proc/self/fd/{fd}")).is_relative_to(self.root):
            os.close(fd)
            raise ToolError(f"{self._display(path)} leads outside the work folder; set it "
                            "up again inside the folder or ask the user")
        return fd

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
            sandbox = [] if self._sandbox is None or args.get("unsandboxed") else self._sandbox
            proc = await asyncio.create_subprocess_exec(
                *sandbox, "sh", "-c", args["command"],
                env=sandbox_environment(os.environ) if sandbox else None,
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
            """Reads only up to what it returns: the file may be huge or endless."""
            selected: list[str] = []
            size, notes = 0, []
            with open(self._open(path, os.O_RDONLY), encoding="utf-8", errors="replace") as f:
                lines = iter(lambda: f.readline(MAX_READ_CHARS + 1), "")
                for number, line in enumerate(lines, 1):
                    if number < offset:
                        continue
                    if number >= offset + limit:
                        notes.append(f"[more lines follow; use offset {number} to read on]")
                        break
                    if size + len(line) > MAX_READ_CHARS:
                        selected.append(line[: MAX_READ_CHARS - size])
                        notes.append(f"[truncated at {MAX_READ_CHARS} characters]")
                        break
                    selected.append(line)
                    size += len(line)
            text = "".join(selected)
            return "\n".join([text, *notes]) if notes else text

        return await asyncio.to_thread(read)

    async def _write_file(self, args: dict[str, Any], ctx: ToolContext) -> str:
        path = self._path(args["path"])
        content: str = args["content"]

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._open(path, os.O_WRONLY | os.O_CREAT), "w", encoding="utf-8") as f:
                f.truncate()
                f.write(content)

        await asyncio.to_thread(write)
        return f"Wrote {len(content)} characters to {self._display(path)}"

    async def _edit_file(self, args: dict[str, Any], ctx: ToolContext) -> str:
        path = self._path(args["path"])
        old, new = args["old_string"], args["new_string"]
        replace_all = args.get("replace_all", False)

        def edit() -> int:
            with open(self._open(path, os.O_RDWR), "r+", encoding="utf-8") as f:
                text = f.read()
                count = text.count(old)
                if count == 1 or (count > 1 and replace_all):
                    f.seek(0)
                    f.truncate()
                    f.write(text.replace(old, new))
            if count == 0:
                raise ToolError(f"old_string not found in {self._display(path)}")
            if count > 1 and not replace_all:
                raise ToolError(
                    f"old_string occurs {count} times in {self._display(path)}; "
                    "add context to make it unique or set replace_all"
                )
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
            for path in _walk(base, cancelled, ctx):
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
            for path in _walk(base, cancelled, ctx):
                if name_glob and not fnmatch(path.name, name_glob):
                    continue
                try:
                    info = path.lstat()  # links and special files are skipped, not followed
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SEARCH_FILE_BYTES:
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


def _walk(base: Path, cancelled: threading.Event, ctx: ToolContext) -> Iterator[Path]:
    """Files under base, skipping hidden directories; sends silent heartbeats meanwhile."""
    last_beat = 0.0
    for dirpath, dirnames, filenames in os.walk(base):
        if cancelled.is_set():
            return
        if (now := time.monotonic()) - last_beat >= HEARTBEAT_INTERVAL:
            ctx.progress("")
            last_beat = now
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
