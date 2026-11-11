"""Sub-sessions: a Tools module a frontend offers (Frontend.tools) so the agent can hand
self-contained tasks to children, headless Kayden processes it starts and drives over
their channel (kayden.frontends.headless). The frontend hosts them, as an app hosts its
sessions: it lists them from `SubSessions.sessions` and opens one with `open`, which gives
a RuntimeAPI for it (kayden.frontends.remote). No UI code here.

A child gets its own data folder (<data dir>/sessions/<conversation>-<id>) with the parent's
config.toml and settings.toml, its spending limit and the conversation's work folder, and
works on its task as a goal. Its permission prompts wait for the user, who answers them
where the child is open. Each child's record (session.json) keeps it listed after a
restart; opening a child that is not running starts it again on its conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import shutil
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from kayden.api import (
    DESCRIPTION_ARG,
    NOTICE_PREFIX,
    AgentState,
    ConversationUpdated,
    EventEnvelope,
    Failure,
    HelpNeeded,
    Message,
    MessageCommitted,
    PermissionRequested,
    PermissionResolved,
    ReasoningDelta,
    Role,
    RuntimeStateError,
    StateChanged,
    Text,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolContext,
    ToolOutput,
    ToolProgress,
    ToolResult,
    Tools,
    ToolSpec,
    ToolStatus,
    UsageUpdated,
)
from kayden.frontends.remote import Channel, RemoteRuntime

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_USD = 1.0
CHECK_IN_S = (5.0, 120.0)  # a child's duty intervals: it continues soon after a turn ends
STOP_GRACE_S = 10.0  # after stdin closes, before the child is killed
RECORD = "session.json"
_SESSION = {"type": "string", "description": "The sub-session's id."}
_ACTIVE = (AgentState.THINKING, AgentState.RUNNING_TOOLS, AgentState.AWAITING_PERMISSION)
_QUIET = (TextDelta, ReasoningDelta, ToolCallDelta, ToolProgress)

START = ToolSpec(
    "start_session",
    "Hand a self-contained task to a sub-session: a separate agent with its own context, "
    "working in the same folder with your tools, so long side work does not fill your "
    "conversation. Describe the task fully: it sees nothing of this conversation. It runs in "
    "the background and returns its report when done, or a question if it needs one. "
    "Several can run at once; the user can open and talk to each. Once the call has "
    "returned, you get a notice whenever it stops working. Start one when the user asked "
    "for the work; when it is your own idea, propose it (propose) instead.",
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
    "Answer a sub-session's question, or tell it more (starting it again if it stopped), "
    "then wait for its report or next question.",
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
                "waiting": "Waiting for an answer", "idle": "Ready", "done": "Done",
                "failed": "Failed", "stopped": "Stopped"}
NOTE_CHARS = 400  # of a child's last reply in a notice to its parent


def working_text(sessions: Sequence[SubSession]) -> str | None:
    """For a status line: how many of these sub-sessions are working, None for none."""
    count = sum(s.running for s in sessions)
    return f"{count} sub-session{'s' if count != 1 else ''} working" if count else None


def message_lines(messages: Sequence[Message]) -> list[tuple[str, str]]:
    """(who, text) for each part of a conversation, for a plain text view."""
    shown = []
    for message in messages:
        for block in message.blocks:
            match block:
                case Text(text=text) if text.strip():
                    who = {Role.USER: "You", Role.ASSISTANT: "Agent"}.get(message.role,
                                                                        message.role)
                    if text.startswith(NOTICE_PREFIX):
                        who = "Kayden"
                    shown.append((who, text))
                case ToolCall(name=name, arguments=args):
                    shown.append(("Step", args.get(DESCRIPTION_ARG) or name))
                case ToolResult(status=ToolStatus.REFUSED, content=content):
                    shown.append(("Step", f"refused: {content}"))
    return shown


@dataclass
class SubSession:
    """One child as the frontend lists it. `state`: starting, working, waiting (for an
    answer to its question), idle (reopened, nothing to do), done, failed or stopped."""

    id: str
    task: str
    conversation_id: str  # the parent conversation
    data_dir: Path
    state: str = "starting"
    report: str | None = None
    question: str | None = None
    failure: str | None = None
    spent: float = 0.0
    child_conversation_id: str | None = None
    title: str | None = None  # the user's name for it; None: its task
    archived: bool = False
    has_goal: bool = False  # its goal is not reached yet
    budget_usd: float = DEFAULT_BUDGET_USD
    step: str | None = field(default=None, compare=False)  # what it is doing now
    approvals: int = field(default=0, compare=False)  # permission prompts open
    messages: list[Message] = field(default_factory=list, compare=False)
    returncode: int | None = field(default=None, compare=False)
    awaited: int = field(default=0, compare=False)  # parent calls waiting on it
    agent_state: AgentState | None = field(default=None, compare=False)
    _changed: asyncio.Event | None = field(default=None, compare=False, repr=False)

    @property
    def running(self) -> bool:
        return self.state in ("starting", "working", "waiting")

    @property
    def name(self) -> str:
        return self.title or self.task

    @property
    def resumable(self) -> bool:
        """Stopped, or ended with Kayden, before its goal was reached."""
        return self.state in ("stopped", "failed", "idle") and self.has_goal

    @property
    def changed(self) -> asyncio.Event:
        """Set on every change; a waiter clears it before it looks again."""
        if self._changed is None:
            self._changed = asyncio.Event()
        return self._changed

    @property
    def summary(self) -> str:
        """Its state for a list row: "Working · Searching the web · $0.04"."""
        parts = ["Needs your approval" if self.approvals else STATE_LABELS[self.state]]
        if self.state == "working" and self.step and not self.approvals:
            parts.append(self.step)
        if self.state == "failed" and self.failure:
            parts.append(self.failure)
        parts.append(f"${self.spent:.2f}")
        return " · ".join(parts)

    def lines(self) -> list[tuple[str, str]]:
        """(who, text) for each part of its conversation this run, for a plain view."""
        return message_lines(self.messages)

    def record(self) -> dict[str, Any]:
        """What session.json keeps."""
        return {k: getattr(self, k) for k in (
            "id", "task", "conversation_id", "state", "report", "question", "failure",
            "spent", "child_conversation_id", "title", "archived", "has_goal", "budget_usd")}


class SubSessions(Tools):
    def __init__(self, data_dir: Path, command: Sequence[str] = (sys.executable, "-m", "kayden"),
                 check_in_s: tuple[float, float] = CHECK_IN_S) -> None:
        self.data_dir = data_dir
        self.command = list(command)
        self.check_in_s = check_in_s
        self.listeners: list[Callable[[], None]] = []  # told of every change
        self.sessions = self._load()
        self._channels: dict[str, Channel] = {}
        self._viewers: dict[str, int] = {}
        self._folder: Path | None = None
        self._next = max((int(s.id) for s in self.sessions if s.id.isdigit()), default=0) + 1
        self._closing: set[asyncio.Task[None]] = set()
        self._saved: dict[str, dict[str, Any]] = {s.id: s.record() for s in self.sessions}
        self._write: asyncio.Future[None] | None = None
        # Notices for parent conversations: a child ended a turn nobody was waiting for.
        self.notes: dict[str, list[str]] = {}

    def of(self, conversation_id: str | None, *, include_archived: bool = False
           ) -> list[SubSession]:
        return [s for s in self.sessions if s.conversation_id == conversation_id
                and (include_archived or not s.archived)]

    def take_notes(self, conversation_id: str) -> list[str]:
        """The notices waiting for this conversation; the frontend sends them to it."""
        return self.notes.pop(conversation_id, [])

    # ── For frontends ─────────────────────────────────────────────────────

    async def open(self, session: SubSession) -> RemoteRuntime:
        """A RuntimeAPI for the child, starting it again when it is not running; call
        release() with it when the view closes."""
        if session.child_conversation_id is None:
            raise RuntimeStateError("this sub-session has not started")
        channel = self._channels.get(session.id)
        if channel is None:
            channel = await self._spawn(session, resume=True)
            self._set(session, state="idle", failure=None)
        self._viewers[session.id] = self._viewers.get(session.id, 0) + 1
        remote = RemoteRuntime(channel)
        try:
            return await remote.attach()
        except BaseException:  # cancelled too: the view never got it
            self.release(session, remote)
            raise

    def release(self, session: SubSession, runtime: RemoteRuntime | None) -> None:
        if runtime is not None:
            runtime.detach()
        self._viewers[session.id] = max(0, self._viewers.get(session.id, 0) - 1)
        if not self._viewers[session.id] and not session.running:
            self._close_channel(session)

    def stop(self, session: SubSession) -> None:
        """Stop a child: the frontend's Stop and stop_session."""
        if session.running:
            self._set(session, state="stopped")
        self._close_channel(session)

    async def resume(self, session: SubSession) -> None:
        """Start it again on its conversation and let it carry on with its goal."""
        channel = self._channels.get(session.id) or await self._spawn(session, resume=True)
        self._set(session, state="working", failure=None)
        await channel.request("activate")

    def rename(self, session: SubSession, title: str) -> None:
        self._set(session, title=title.strip() or None)

    def archive(self, session: SubSession, archived: bool) -> None:
        self._set(session, archived=archived)

    async def delete(self, session: SubSession) -> None:
        """Stop it and erase its folder for good."""
        channel = self._channels.pop(session.id, None)
        if channel is not None:
            await channel.close(STOP_GRACE_S)
        if self._write is not None:
            await asyncio.gather(self._write, return_exceptions=True)
        await asyncio.to_thread(shutil.rmtree, session.data_dir, True)
        self.sessions.remove(session)
        self._notify()

    # ── Tools ─────────────────────────────────────────────────────────────

    async def list_tools(self) -> Sequence[ToolSpec]:
        return (START, MESSAGE, STOP)

    async def use_folder(self, folder: Path | None) -> None:
        self._folder = folder

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutput:
        args = call.arguments
        if call.name == START.name:
            session = await self._new(args["task"], args.get("budget_usd", DEFAULT_BUDGET_USD),
                                      ctx.conversation_id)
            try:
                await self._launch(session)
            except asyncio.CancelledError:  # stopped while it was starting
                self.stop(session)
                raise
            if session.state == "failed":
                return ToolOutput(self._outcome(session), is_error=True)
            return await self._wait(session, ctx)
        session = next((s for s in self.sessions if s.id == args["session"]), None)
        if session is None:
            return ToolOutput(f"There is no sub-session {args['session']!r}.", is_error=True)
        if call.name == STOP.name:
            self.stop(session)
            return ToolOutput(f"Sub-session {session.id} stopped.")
        if session.child_conversation_id is None:
            return ToolOutput(f"Sub-session {session.id} never started.", is_error=True)
        try:
            channel = self._channels.get(session.id) or await self._spawn(session, resume=True)
            self._set(session, state="working", question=None, failure=None)
            await channel.request("send", text=args["text"])
        except asyncio.CancelledError:
            self.stop(session)
            raise
        return await self._wait(session, ctx)

    async def aclose(self) -> None:
        for session in self.sessions:
            if session.running:
                self._set(session, state="stopped")
            self._close_channel(session)
        if self._closing:
            await asyncio.wait(self._closing)
        if self._write is not None:
            await asyncio.gather(self._write, return_exceptions=True)

    # ── Children ──────────────────────────────────────────────────────────

    async def _new(self, task: str, budget: float, conversation_id: str) -> SubSession:
        session_id = str(self._next)
        self._next += 1
        folder = self.data_dir / "sessions" / f"{conversation_id[:8]}-{session_id}"
        session = SubSession(session_id, task, conversation_id, folder, has_goal=True,
                             budget_usd=budget)
        await asyncio.to_thread(folder.mkdir, parents=True)
        self.sessions.append(session)
        self._set(session)
        return session

    async def _launch(self, session: SubSession) -> None:
        self._set(session, state="starting")
        try:
            await asyncio.to_thread(self._prepare, session.data_dir, session.budget_usd)
            channel = await self._spawn(session, resume=False)
            info = await channel.request("start")
            session.child_conversation_id = info["id"]
            await channel.request("set_duty", duty={
                "instruction": session.task, "kind": "goal",
                "min_interval_s": self.check_in_s[0], "max_interval_s": self.check_in_s[1]})
        except (OSError, RuntimeError, ValueError) as e:
            self._set(session, state="failed", failure=self._why(session, e))
            self._close_channel(session)
            return
        if session.state == "starting":
            self._set(session, state="working")

    def _prepare(self, folder: Path, budget: float) -> None:
        """The child's data folder: the parent's configuration, its limit and folder."""
        folder.mkdir(parents=True, exist_ok=True)
        config = self.data_dir / "config.toml"
        if config.exists():
            shutil.copyfile(config, folder / "config.toml")
        settings_file = self.data_dir / "settings.toml"
        settings = tomllib.loads(settings_file.read_text()) if settings_file.exists() else {}
        settings["budget"] = {"max_cost_usd": budget}
        if self._folder is not None:
            settings["work_folder"] = str(self._folder)
        (folder / "settings.toml").write_text(tomli_w.dumps(settings))

    async def _spawn(self, session: SubSession, *, resume: bool) -> Channel:
        command = [*self.command, "--headless", "--data-dir", str(session.data_dir)]
        if resume and session.child_conversation_id is not None:
            command += ["--conversation", session.child_conversation_id]
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=self._folder or session.data_dir,
            limit=16 * 1024 * 1024)
        channel = Channel(process)
        channel.listeners.append(lambda envelope: self._event(session, channel, envelope))
        self._channels[session.id] = channel
        return channel

    async def _wait(self, session: SubSession, ctx: ToolContext) -> ToolOutput:
        """Until it reports, asks, fails or stops; its current step is the progress line."""
        shown = None
        session.awaited += 1
        try:
            while session.state in ("starting", "working"):
                session.changed.clear()
                step = "Needs your approval" if session.approvals else session.step
                if step and step != shown:
                    shown = step
                    ctx.progress(step)
                await session.changed.wait()
        except asyncio.CancelledError:
            self.stop(session)
            raise
        finally:
            session.awaited -= 1
        if session.state == "done" and not self._viewers.get(session.id):
            self._close_channel(session)
        return ToolOutput(self._outcome(session), is_error=session.state == "failed")

    def _outcome(self, session: SubSession) -> str:
        match session.state:
            case "done":
                return f"Sub-session {session.id} finished. Report: {session.report}"
            case "waiting":
                return (f"Sub-session {session.id} asks: {session.question}\nAnswer with "
                        f"message_session(session=\"{session.id}\", text=...).")
            case "stopped":
                return f"Sub-session {session.id} was stopped."
        return f"Sub-session {session.id} failed: {session.failure}"

    def _event(self, session: SubSession, channel: Channel,
               envelope: EventEnvelope | None) -> None:
        if envelope is None:  # the process ended
            if self._channels.get(session.id) is channel:
                del self._channels[session.id]
                task = asyncio.create_task(self._ended(session, channel))
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)
            return
        if isinstance(envelope.event, _QUIET):
            return  # streaming: nothing a list shows
        match envelope.event:
            case MessageCommitted(message=message):
                session.messages.append(message)
                for block in message.blocks:
                    if isinstance(block, ToolCall):
                        if block.name == "finish_goal":
                            session.report = block.arguments.get("report")
                        session.step = block.arguments.get(DESCRIPTION_ARG) or block.name
                    elif isinstance(block, Text) and message.role == Role.ASSISTANT:
                        session.step = block.text.strip().splitlines()[0][:120] or None
            case PermissionRequested():
                session.approvals += 1
            case PermissionResolved():
                session.approvals = max(0, session.approvals - 1)
            case HelpNeeded(question=question) if question is not None:
                session.question = question
            case ConversationUpdated(info=info) if info.id == session.child_conversation_id:
                session.has_goal = info.duty is not None
            case UsageUpdated(snapshot=usage):
                session.spent = usage.usage.cost_usd or 0.0
            case Failure(message=message):
                session.failure = message
            case StateChanged(state=state):
                ended_turn = session.agent_state in _ACTIVE and state not in _ACTIVE
                session.agent_state = state
                session.state = self._state_after(session, state)
                # Between a goal's check-ins it is still working: no notice until it stops.
                if ended_turn and session.state != "working" and not session.awaited:
                    self._note(session)
        self._set(session)

    def _note(self, session: SubSession) -> None:
        """A notice for the parent: the child ended a turn while no call of the parent's
        waited for it (the user talked to it, or it carried on with its goal)."""
        reply = next((b.text for m in reversed(session.messages) if m.role == Role.ASSISTANT
                      for b in m.blocks if isinstance(b, Text) and b.text.strip()), "")
        if len(reply) > NOTE_CHARS:
            reply = reply[:NOTE_CHARS] + "…"
        note = (f"{NOTICE_PREFIX}Sub-session {session.id} ({session.name[:80]}) ended a "
                f"turn: {STATE_LABELS[session.state].lower()}.")
        if session.state == "waiting":
            note += f" It asks: {session.question}"
        elif session.state == "done":
            note += f" Report: {session.report}"
        elif reply:
            note += f" Its last reply: {reply}"
        self.notes.setdefault(session.conversation_id, []).append(note)

    def _state_after(self, session: SubSession, state: AgentState) -> str:
        if session.state == "stopped":
            return "stopped"
        if state in _ACTIVE:
            return "working"
        if state == AgentState.FAILED:
            return "failed"
        if state in (AgentState.IDLE, AgentState.STOPPED):
            if session.question is not None and session.has_goal:
                return "waiting"
            if session.has_goal:
                return "working"  # between a goal's check-ins
            return "done" if session.report is not None else "idle"
        return session.state

    async def _ended(self, session: SubSession, channel: Channel) -> None:
        session.returncode = await channel.close(STOP_GRACE_S)
        if session.running:  # it ended by itself
            self._set(session, state="failed", failure=self._why(session, None, channel))
        session.approvals = 0
        self._set(session)

    def _close_channel(self, session: SubSession) -> None:
        if (channel := self._channels.pop(session.id, None)) is not None:
            task = asyncio.create_task(self._ended(session, channel))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    def _why(self, session: SubSession, error: Exception | None,
             channel: Channel | None = None) -> str:
        channel = channel or self._channels.get(session.id)
        tail = [line for line in (channel.stderr if channel else ()) if line.startswith("kayden:")]
        if tail:
            return tail[-1].removeprefix("kayden:").strip()
        return str(error) if error is not None else "the process ended"

    # ── Records and listeners ─────────────────────────────────────────────

    def _load(self) -> list[SubSession]:
        sessions = []
        for record in sorted((self.data_dir / "sessions").glob(f"*/{RECORD}")):
            try:
                data = json.loads(record.read_text())
                session = SubSession(data_dir=record.parent, **data)
            except (OSError, ValueError, TypeError):
                logger.warning("unreadable sub-session record %s", record)
                continue
            if session.running or session.state == "starting":  # it stopped with the last run
                session.state = "stopped"
            sessions.append(session)
        return sorted(sessions, key=lambda s: int(s.id) if s.id.isdigit() else 0)

    def _set(self, session: SubSession, **changes: Any) -> None:
        for name, value in changes.items():
            setattr(session, name, value)
        record = session.record()
        if record != self._saved.get(session.id) and session.data_dir.exists():
            self._saved[session.id] = record
            path = session.data_dir / RECORD
            self._write = asyncio.ensure_future(self._save(self._write, path, record))
        session.changed.set()
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                logger.exception("sub-session listener failed")

    @staticmethod
    async def _save(previous: asyncio.Future[None] | None, path: Path,
                    record: dict[str, Any]) -> None:
        """Write a record after the one before it, off the event loop."""
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        with contextlib.suppress(OSError):  # a record is for listing only
            await asyncio.to_thread(path.write_text, json.dumps(record))

    def _notify(self) -> None:
        for listener in list(self.listeners):
            listener()
