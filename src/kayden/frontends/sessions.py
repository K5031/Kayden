"""Sub-sessions: a Tools module a frontend offers (Frontend.tools) so the agent can hand
self-contained tasks to children, headless Kayden processes it starts and drives over
their channel (kayden.frontends.headless). The frontend hosts them, as an app hosts its
sessions: it lists them and shows their work from `SubSessions.sessions`. No UI code here.

A child gets its own data folder (<data dir>/sessions/<id>) with the parent's config.toml
and settings.toml, its spending limit and the conversation's work folder, and works on its
task as a goal. A permission prompt in a child is refused: nobody watches it.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import math
import shutil
import sys
import tomllib
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import tomli_w

from kayden.api import DESCRIPTION_ARG, ToolCall, ToolContext, ToolOutput, Tools, ToolSpec

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_USD = 1.0
CHECK_IN_S = (5.0, 120.0)  # a child's duty intervals: it continues soon after a turn ends
STOP_GRACE_S = 10.0  # after stdin closes, before the child is killed
_STDERR_LINES = 20
_SESSION = {"type": "string", "description": "The sub-session's id."}

START = ToolSpec(
    "start_session",
    "Hand a self-contained task to a sub-session: a separate agent with its own context, "
    "working in the same folder with your tools, so long side work does not fill your "
    "conversation. Describe the task fully: it sees nothing of this conversation. It runs in "
    "the background and returns its report when done, or a question if it needs one. "
    "Several can run at once.",
    {"type": "object",
     "properties": {
         "task": {"type": "string", "minLength": 1},
         "budget_usd": {"type": "number", "exclusiveMinimum": 0,
                        "default": DEFAULT_BUDGET_USD,
                        "description": "The most it may spend, in US dollars."}},
     "required": ["task"]},
    timeout=math.inf,
)
MESSAGE = ToolSpec(
    "message_session",
    "Answer a sub-session's question, or tell it more, then wait for its report or next "
    "question.",
    {"type": "object", "properties": {"session": _SESSION, "text": {"type": "string",
                                                                    "minLength": 1}},
     "required": ["session", "text"]},
    timeout=math.inf,
)
STOP = ToolSpec(
    "stop_session",
    "Stop a sub-session that is no longer needed.",
    {"type": "object", "properties": {"session": _SESSION}, "required": ["session"]},
)


STATE_LABELS = {"starting": "Starting", "working": "Working",
                "waiting": "Waiting for an answer", "done": "Done", "failed": "Failed",
                "stopped": "Stopped"}


def working_text(sessions: Sequence[SubSession]) -> str | None:
    """For a status line: how many of these sub-sessions are working, None for none."""
    count = sum(s.running for s in sessions)
    return f"{count} sub-session{'s' if count != 1 else ''} working" if count else None


class SubSession:
    """One child as the frontend shows it. `state`: starting, working, waiting (for an
    answer), done, failed or stopped."""

    def __init__(self, session_id: str, task: str, conversation_id: str, data_dir: Path
                 ) -> None:
        self.id, self.task = session_id, task
        self.conversation_id, self.data_dir = conversation_id, data_dir
        self.state = "starting"
        self.step: str | None = None  # what it is doing now
        self.spent = 0.0
        self.report: str | None = None
        self.question: str | None = None
        self.failure: str | None = None
        self.refused: list[str] = []
        self.returncode: int | None = None
        self.messages: list[dict[str, Any]] = []  # as the channel sends them
        self._goal_ended = False
        self._changed = asyncio.Condition()

    @property
    def running(self) -> bool:
        return self.state in ("starting", "working", "waiting")

    @property
    def summary(self) -> str:
        """Its state for a list row: "Working · Searching the web · $0.04"."""
        parts = [STATE_LABELS[self.state]]
        if self.running and self.step:
            parts.append(self.step)
        if self.state == "failed" and self.failure:
            parts.append(self.failure)
        parts.append(f"${self.spent:.2f}")
        return " · ".join(parts)

    def lines(self) -> list[tuple[str, str]]:
        """(who, text) for each part of its conversation, for a frontend to show."""
        shown = []
        for message in self.messages:
            role = message["role"]
            for block in message["blocks"]:
                match block["type"]:
                    case "Text" if block["text"].strip():
                        who = {"user": "Task", "assistant": "Agent"}.get(role, role)
                        if block["text"].startswith("[Runtime notice]"):
                            who = "Kayden"
                        shown.append((who, block["text"]))
                    case "ToolCall":
                        args = block["arguments"]
                        shown.append(("Step", args.get(DESCRIPTION_ARG) or block["name"]))
                    case "ToolResult" if block["status"] == "refused":
                        shown.append(("Step", f"refused: {block['content']}"))
        return shown


class _Child:
    """The child's process and its JSON-RPC channel."""

    def __init__(self, process: asyncio.subprocess.Process,
                 on_event: Callable[[dict[str, Any]], None]) -> None:
        self.process = process
        self._on_event = on_event
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self.stderr: deque[str] = deque(maxlen=_STDERR_LINES)
        self.reader = asyncio.create_task(self._read())
        self.errors = asyncio.create_task(self._read_stderr())

    async def request(self, method: str, **params: Any) -> Any:
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        assert self.process.stdin is not None
        line = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                           "params": params})
        self.process.stdin.write(line.encode() + b"\n")
        await self.process.stdin.drain()
        return await future

    async def _read(self) -> None:
        assert self.process.stdout is not None
        while line := await self.process.stdout.readline():
            message = json.loads(line)
            if message.get("method") == "event":
                self._on_event(message["params"]["event"])
            elif (future := self._pending.pop(message.get("id"), None)) is not None:
                if "error" in message:
                    future.set_exception(RuntimeError(message["error"]["message"]))
                else:
                    future.set_result(message.get("result"))
        for future in self._pending.values():
            future.set_exception(RuntimeError("the sub-session ended"))

    async def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        while line := await self.process.stderr.readline():
            self.stderr.append(line.decode(errors="replace").rstrip())

    async def close(self) -> int:
        """Close its stdin (it quits as on closing the window); kill it after the grace."""
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), STOP_GRACE_S)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
        await asyncio.gather(self.reader, self.errors, return_exceptions=True)
        assert self.process.returncode is not None
        return self.process.returncode


class SubSessions(Tools):
    def __init__(self, data_dir: Path, command: Sequence[str] = (sys.executable, "-m", "kayden"),
                 check_in_s: tuple[float, float] = CHECK_IN_S) -> None:
        self.data_dir = data_dir
        self.check_in_s = check_in_s
        self.command = list(command)
        self.sessions: list[SubSession] = []
        self.listeners: list[Callable[[], None]] = []  # told of every change
        self._children: dict[str, _Child] = {}
        self._folder: Path | None = None
        self._ids = itertools.count(1)
        self._closing: set[asyncio.Task[None]] = set()

    def of(self, conversation_id: str | None) -> list[SubSession]:
        return [s for s in self.sessions if s.conversation_id == conversation_id]

    def stop(self, session: SubSession) -> None:
        """Stop a child; for the frontend's Stop button and stop_session."""
        if session.running:
            self._set(session, state="stopped")
        if (child := self._children.pop(session.id, None)) is not None:
            task = asyncio.create_task(self._close(session, child))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    # ── Tools ─────────────────────────────────────────────────────────────

    async def list_tools(self) -> Sequence[ToolSpec]:
        return (START, MESSAGE, STOP)

    async def use_folder(self, folder: Path | None) -> None:
        self._folder = folder

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutput:
        args = call.arguments
        if call.name == START.name:
            session = await self._start(args["task"], args.get("budget_usd", DEFAULT_BUDGET_USD),
                                        ctx.conversation_id)
            if session.state == "failed":
                return ToolOutput(self._outcome(session), is_error=True)
            return await self._wait(session, ctx)
        session = next((s for s in self.sessions if s.id == args["session"]), None)
        if session is None:
            return ToolOutput(f"There is no sub-session {args['session']!r}.", is_error=True)
        if call.name == STOP.name:
            self.stop(session)
            return ToolOutput(f"Sub-session {session.id} stopped.")
        child = self._children.get(session.id)
        if child is None or session.state != "waiting" and session.state != "working":
            return ToolOutput(f"Sub-session {session.id} is {session.state}; start a new "
                              "one for more work.", is_error=True)
        self._set(session, state="working", question=None)
        await child.request("send", text=args["text"])
        return await self._wait(session, ctx)

    async def aclose(self) -> None:
        for session in list(self.sessions):
            self.stop(session)
        if self._closing:
            await asyncio.wait(self._closing)

    # ── Children ──────────────────────────────────────────────────────────

    async def _start(self, task: str, budget: float, conversation_id: str) -> SubSession:
        session_id = str(next(self._ids))
        folder = self.data_dir / "sessions" / f"{conversation_id[:8]}-{session_id}"
        session = SubSession(session_id, task, conversation_id, folder)
        self.sessions.append(session)
        self._notify()
        try:
            await asyncio.to_thread(self._prepare, folder, budget)
            process = await asyncio.create_subprocess_exec(
                *self.command, "--headless", "--data-dir", str(folder),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=self._folder or folder,
                limit=16 * 1024 * 1024)
        except OSError as e:
            self._set(session, state="failed", failure=f"it could not start: {e}")
            return session
        child = _Child(process, lambda event: self._event(session, event))
        self._children[session_id] = child
        try:
            await child.request("start")
            await child.request("set_duty", duty={
                "instruction": task, "kind": "goal",
                "min_interval_s": self.check_in_s[0], "max_interval_s": self.check_in_s[1]})
        except RuntimeError as e:
            self._set(session, state="failed", failure=self._why(child, e))
            self.stop(session)
            return session
        if session.state == "starting":
            self._set(session, state="working")
        return session

    def _prepare(self, folder: Path, budget: float) -> None:
        """The child's data folder: the parent's configuration, its limit and folder."""
        folder.mkdir(parents=True)
        config = self.data_dir / "config.toml"
        if config.exists():
            shutil.copyfile(config, folder / "config.toml")
        settings_file = self.data_dir / "settings.toml"
        settings = tomllib.loads(settings_file.read_text()) if settings_file.exists() else {}
        settings["budget"] = {"max_cost_usd": budget}
        if self._folder is not None:
            settings["work_folder"] = str(self._folder)
        (folder / "settings.toml").write_text(tomli_w.dumps(settings))

    async def _wait(self, session: SubSession, ctx: ToolContext) -> ToolOutput:
        """Until it reports, asks, fails or stops; its current step is the progress line."""
        shown = None
        try:
            async with session._changed:
                while session.state in ("starting", "working"):
                    if session.step and session.step != shown:
                        shown = session.step
                        ctx.progress(shown)
                    await session._changed.wait()
        except asyncio.CancelledError:
            self.stop(session)
            raise
        if session.state == "done":
            self.stop(session)
        return ToolOutput(self._outcome(session), is_error=session.state == "failed")

    def _outcome(self, session: SubSession) -> str:
        refused = (f"\nSteps refused, since nobody could approve them: "
                   f"{', '.join(session.refused)}."
                   if session.refused else "")
        match session.state:
            case "done":
                return f"Sub-session {session.id} finished. Report: {session.report}{refused}"
            case "waiting":
                return (f"Sub-session {session.id} asks: {session.question}\nAnswer with "
                        f"message_session(session=\"{session.id}\", text=...).{refused}")
            case "stopped":
                return f"Sub-session {session.id} was stopped.{refused}"
        return f"Sub-session {session.id} failed: {session.failure}{refused}"

    def _event(self, session: SubSession, event: dict[str, Any]) -> None:
        match event["type"]:
            case "MessageCommitted":
                message = event["message"]
                session.messages.append(message)
                for block in message["blocks"]:
                    if block["type"] == "ToolCall":
                        if block["name"] == "finish_goal":
                            session.report = block["arguments"].get("report")
                        session.step = (block["arguments"].get(DESCRIPTION_ARG)
                                        or block["name"])
                    elif block["type"] == "Text" and message["role"] == "assistant":
                        session.step = block["text"].strip().splitlines()[0][:120] or None
            case "PermissionRequested":
                request = event["request"]
                session.refused.append(request["call"]["name"])
                child = self._children.get(session.id)
                if child is not None:
                    task = asyncio.create_task(child.request(
                        "resolve_permission", request_id=request["id"], decision="deny"))
                    task.add_done_callback(lambda t: t.exception())
            case "HelpNeeded" if event["question"] is not None:
                session.question = event["question"]
            case "ConversationUpdated" if event["info"]["duty"] is None:
                session._goal_ended = True
            case "UsageUpdated":
                session.spent = event["snapshot"]["usage"]["cost_usd"] or 0.0
            case "Failure":
                session.failure = event["message"]
            case "StateChanged" if session.running:
                state = event["state"]
                if state in ("idle", "stopped") and session._goal_ended:
                    session.state = "done"
                elif state == "idle" and session.question is not None:
                    session.state = "waiting"
                elif state == "failed":
                    session.state = "failed"
        self._changed_now(session)

    async def _close(self, session: SubSession, child: _Child) -> None:
        session.returncode = await child.close()
        if session.running:  # it ended by itself
            self._set(session, state="failed", failure=self._why(child, None))
        self._changed_now(session)

    def _why(self, child: _Child, error: Exception | None) -> str:
        tail = [line for line in child.stderr if line.startswith("kayden:")]
        if tail:
            return tail[-1].removeprefix("kayden:").strip()
        return str(error) if error is not None else "the process ended"

    def _set(self, session: SubSession, **changes: Any) -> None:
        for name, value in changes.items():
            setattr(session, name, value)
        self._changed_now(session)

    def _changed_now(self, session: SubSession) -> None:
        async def wake() -> None:
            async with session._changed:
                session._changed.notify_all()

        with contextlib.suppress(RuntimeError):  # no loop: nothing waits
            asyncio.get_running_loop().create_task(wake())
        self._notify()

    def _notify(self) -> None:
        for listener in self.listeners:
            try:
                listener()
            except Exception:
                logger.exception("sub-session listener failed")
