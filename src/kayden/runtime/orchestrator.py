"""The Kayden runtime: cycle loop, tool scheduling, permissions, budgets, stop."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema

from kayden.api import (
    BUILTIN_TOOLS,
    DESCRIPTION_ARG,
    DUTY_TOOLS,
    PERMISSION_CEILING_ENV,
    SKILL_TOOL,
    AgentConfig,
    AgentState,
    ArchiveHit,
    Budget,
    BudgetExhausted,
    CallId,
    CompactionFinished,
    CompactionStarted,
    Completed,
    ContextOverflowError,
    ContextRequest,
    ConversationDeleted,
    ConversationId,
    ConversationInfo,
    ConversationUpdated,
    Core,
    CoreError,
    Decision,
    Duty,
    DutyKind,
    DutyScheduled,
    EventEnvelope,
    Failure,
    FailureKind,
    FrontendInfo,
    HelpNeeded,
    InferenceRequest,
    MaintenanceFinished,
    MaintenanceStarted,
    MemoriesChanged,
    Memory,
    MemoryNote,
    MemoryView,
    Message,
    MessageCommitted,
    MessageQueued,
    ModelInfo,
    ModelRetry,
    ModelSelected,
    OperationInfo,
    OperationResult,
    PermissionPolicy,
    PermissionRequest,
    PermissionRequested,
    PermissionResolved,
    PlanItem,
    Proposal,
    ProposalMade,
    ProposalResolved,
    ReasoningDelta,
    Role,
    RuntimeEvent,
    RuntimeSnapshot,
    RuntimeStateError,
    Settings,
    SettingsChanged,
    SettingsUpdate,
    ShuttingDown,
    Skill,
    StartOptions,
    StateChanged,
    StopReason,
    StopStalled,
    Text,
    TextDelta,
    TokenCounter,
    ToolBackgrounded,
    ToolCall,
    ToolCallDelta,
    ToolFinished,
    ToolOutput,
    ToolProgress,
    ToolResult,
    Tools,
    ToolSpec,
    ToolStarted,
    ToolStatus,
    TransientCoreError,
    Usage,
    UsageReport,
    UsageUpdated,
)
from kayden.runtime import duty as duties
from kayden.runtime import skills as skill_files
from kayden.runtime.budget import BudgetTracker, calibrated, cost_of, estimate_tokens, has_prices
from kayden.runtime.config import (
    ACTIVATION_NOTICE,
    Restart,
    RuntimeConfig,
)
from kayden.runtime.events import EventBus, EventLog
from kayden.runtime.instance import InstanceLock
from kayden.runtime.modules import ModuleSettings
from kayden.runtime.operations import Context, Operation
from kayden.runtime.review import MAX_OUTPUT_TOKENS as REVIEW_OUTPUT_TOKENS
from kayden.runtime.review import (
    REVIEW_PROMPT,
    Verdict,
    always_ask,
    parse,
    review_messages,
    user_requests,
)

logger = logging.getLogger(__name__)

_RUNNING = (AgentState.THINKING, AgentState.RUNNING_TOOLS, AgentState.AWAITING_PERMISSION)
_OP_STATUS_MAX_WAIT_S = 60
# The description every offered tool takes (api.DESCRIPTION_ARG).
_DESCRIPTION_SCHEMA = {
    "type": "string",
    "description": "What this step does, in a few plain words the user understands without "
                   "knowing the tool, e.g. \"Search the web for flights to Oslo\". The user "
                   "sees this instead of the tool and its input.",
}
REVIEW_HISTORY = 200  # newest messages searched for the user's words in a permission review
USAGE_FILE = "usage.json"
EVENT_LOG_FILE = "events.jsonl"
_NO_MEMORY_EDITING = "this memory module cannot edit memories"
_NO_DELETION = "this memory module cannot delete conversations"
_NO_DUTIES = "this memory module cannot keep duties"
_RUNTIME_TOOLS = frozenset(BUILTIN_TOOLS) | frozenset(DUTY_TOOLS) | {SKILL_TOOL.name}


def _stamped(message: Message) -> Message:
    """A user message as the model sees it: its text starts with when it was sent, from
    the stored time, so the request stays the same from one turn to the next."""
    if message.role != Role.USER:
        return message
    for i, block in enumerate(message.blocks):
        if isinstance(block, Text):
            sent = datetime.fromtimestamp(message.created_at).astimezone()
            stamp = f"[Sent {sent.strftime('%a %Y-%m-%d %H:%M')} {sent.isoformat()[-6:]}] "
            blocks = (*message.blocks[:i], Text(stamp + block.text), *message.blocks[i + 1:])
            return replace(message, blocks=blocks)
    return message


def _new_id() -> str:
    return uuid.uuid4().hex


def _effective_policy(configured: PermissionPolicy,
                      ceiling: PermissionPolicy | None) -> PermissionPolicy:
    return configured.clamp(ceiling) if ceiling is not None else configured


class _MemoryContext:
    """ToolContext for a Memory tool: quick, so progress is not shown."""

    def __init__(self, call_id: CallId, conversation_id: ConversationId) -> None:
        self.call_id = call_id
        self.conversation_id = conversation_id

    def progress(self, text: str) -> None:
        pass


class _AppendFailed(Exception):
    pass


class _Stopped(Exception):
    pass


class _BudgetHit(Exception):
    pass


@dataclass
class _Stream:
    """Accumulated state of one model attempt."""

    input_estimate: int
    started: bool = False  # at least one stream event arrived
    text: list[str] = field(default_factory=list)
    output_estimate: int = 0
    report: Usage | None = None
    completed: Completed | None = None

    def estimate(self) -> Usage:
        """Per dimension, the larger of the last report and the runtime's own estimate."""
        r = self.report or Usage()
        return Usage(
            max(r.input_tokens, self.input_estimate),
            max(r.output_tokens, self.output_estimate),
            r.cached_tokens,
            r.cost_usd,
        )


class _Completion:
    """Metered one-shot inference handed to Memory.maintain."""

    def __init__(self, runtime: Runtime):
        self._rt = runtime
        self.started = False
        self.compacting: bool | None = None  # whether the compacting call returned a summary

    def max_input_tokens(self, *, light: bool = False) -> int:
        return self._rt._window(self._rt._maintenance_info() if light else self._rt._info())

    async def __call__(self, system: str, messages: Sequence[Message], *, max_tokens: int,
                       light: bool = False, compacting: bool = False) -> str:
        rt = self._rt
        if reason := rt._check_budget("maintenance"):
            raise BudgetExhausted(reason)
        assert rt._conversation is not None
        if not self.started:
            self.started = True
            rt._emit(MaintenanceStarted(rt._conversation.id))
        if compacting and self.compacting is None:
            self.compacting = False
            rt._emit(CompactionStarted(rt._conversation.id))
        output = await self._request(system, messages, max_tokens, light)
        if compacting and output:
            self.compacting = True
        return output

    async def _request(self, system: str, messages: Sequence[Message], max_tokens: int,
                       light: bool) -> str:
        rt = self._rt
        info = rt._maintenance_info() if light else rt._info()
        if info.max_output_tokens is not None:
            max_tokens = min(max_tokens, info.max_output_tokens)
        request = InferenceRequest(info.id, system, list(messages), (), max_tokens,
                                   maintenance=True)
        attempt = 0
        while True:
            attempt += 1
            try:
                completed = await rt._attempt(request, info, foreground=False)
            except _BudgetHit as e:
                raise BudgetExhausted(str(e)) from None
            except TransientCoreError as e:
                if attempt > rt._config.max_retries:
                    raise
                delay = rt._backoff(attempt, e)
                rt._log.decision("maintenance_retry", attempt=attempt, delay_s=delay, error=str(e))
                await asyncio.sleep(delay)
                continue
            except CoreError:
                raise
            except Exception as e:
                raise CoreError(f"{type(e).__name__}: {e}") from e
            return "".join(b.text for b in completed.message.blocks if isinstance(b, Text))


class Runtime:
    """Implements RuntimeAPI. One agent per process."""

    def __init__(self, *, core: Core, memory: Memory, tools: Sequence[Tools],
                 frontend: FrontendInfo, config: RuntimeConfig,
                 lock: InstanceLock | None = None):
        self._core = core
        self._memory = memory
        self._tools = list(tools)  # every Tools module, in the order their tools are offered
        self._frontend = frontend
        self._config = config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        # Read before exporting our own policy, which must not clamp later changes.
        self._ceiling = PermissionPolicy.inherited_ceiling()
        self._permissions = _effective_policy(config.permissions, self._ceiling)
        os.environ[PERMISSION_CEILING_ENV] = self._permissions.value
        self._log = EventLog()
        self._bus = EventBus(config.subscriber_queue_size, self._log)
        self._budget = BudgetTracker(config.budget)

        self._started = False
        self._lock = lock  # taken before the first Memory call (_hold_lock)
        self._agent_config: AgentConfig | None = None
        self._conversation: ConversationInfo | None = None
        self._draft = False  # the open conversation is new and not stored yet
        self._models: dict[str, ModelInfo] = {}
        self._model: str | None = config.model
        self._calibration: dict[str, float] = {}

        self._base = AgentState.NOT_STARTED  # state outside a cycle
        self._phase = AgentState.THINKING  # state inside a cycle
        self._emitted = (AgentState.NOT_STARTED, 0)
        self._cycle_requested = False
        self._cycle_active = False
        self._cycle_task: asyncio.Task[None] | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._stop_event = asyncio.Event()
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_abandoned: list[CallId] = []
        self._closed = False
        self._close_task: asyncio.Future[None] | None = None

        self._mem_lock = asyncio.Lock()
        self._backlog: deque[tuple[Message, list[RuntimeEvent], StopReason | None]] = deque()
        self._inbox: list[MessageQueued] = []
        self._pending_results: list[tuple[OperationResult, ToolFinished]] = []
        self._ops: dict[CallId, Operation] = {}  # running
        self._known_ops: dict[CallId, Operation] = {}  # launched by this process
        self._prompts: dict[str, tuple[PermissionRequest, asyncio.Future[Decision]]] = {}
        # Tool name -> whether the grant also covers calls that reach outside the work
        # folder: one given for a confined call covers confined calls only.
        self._grants: dict[str, bool] = {}
        self._memory_tools: set[str] = set()  # offered by Memory this request
        self._skills: dict[str, skill_files.Loaded] = {}  # found for this request
        # The update_plan call that wrote the open conversation's plan; None when a person
        # did, or it is not known (the conversation was opened again).
        self._plan_call: CallId | None = None
        self._skill_problems: list[str] = []
        self._tool_owner: dict[str, Tools] = {}  # the Tools module that offered each name
        self._own_description: set[str] = set()  # tools whose schema declares DESCRIPTION_ARG
        self._wake_set: set[CallId] | None = None
        self._immediate_wake = False
        self._unseen = False
        self._duty_timer: asyncio.Task[None] | None = None
        self._duty_wake_at: float | None = None
        self._duty_cause: str | None = None  # why the next cycle was requested for a duty
        # Without a duty: steps due up to this time have reminded, or fell due while the
        # conversation was closed and wait for the user.
        self._reminded = 0.0
        self._help: str | None = None  # the agent's open question (ask_for_help)
        # Proposals waiting for the user, by the propose call's id (also their operation's).
        self._proposals: dict[CallId, tuple[Proposal, asyncio.Future[bool]]] = {}
        self._proposal_ops: set[CallId] = set()  # operations that are proposals
        self._proposal_done = False  # a proposal's outcome the model has not seen
        self._uncounted: set[CallId] = set()  # operations not counted as activity yet
        self._specs: dict[str, ToolSpec] = {}  # offered in the latest request
        self._flush_tasks: set[asyncio.Task[None]] = set()
        self._duration_task: asyncio.Task[None] | None = None
        self._inbox_before_stop = 0
        self._settings_lock = asyncio.Lock()
        self._modules = ModuleSettings(config.data_dir, config.modules, frontend.kind)
        self.restart_request: Restart | None = None  # set by restart(), read by the launcher

    # ── Observation ──────────────────────────────────────────────────────

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._bus.subscribe()

    def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            seq=self._bus.seq,
            state=self._state(),
            conversation=self._conversation,
            config=self._agent_config,
            model=self._model,
            usage=self._budget.snapshot(),
            operations=[OperationInfo(op.call, op.started_at, op.last_progress, op.backgrounded)
                        for op in self._ops.values() if op.call.id not in self._proposals],
            pending_permissions=[req for req, _ in self._prompts.values()],
            queued_messages=list(self._inbox),
            duty_wake_at=self._duty_wake_at,
            help_needed=self._help,
            proposals=tuple(p for p, _ in self._proposals.values()),
        )

    async def list_models(self) -> Sequence[ModelInfo]:
        return await self._core.list_models()

    async def list_tools(self) -> Sequence[ToolSpec]:
        return list((await self._offered_tools()).values())

    async def list_conversations(self, *, include_archived: bool = False
                                 ) -> Sequence[ConversationInfo]:
        self._hold_lock()
        return await self._memory.list_conversations(include_archived=include_archived)

    async def history(self, conversation_id: ConversationId, *, before: str | None = None,
                      limit: int | None = None) -> Sequence[Message]:
        self._hold_lock()
        if self._is_draft(conversation_id):
            return []
        return await self._memory.history(conversation_id, before=before, limit=limit)

    async def search_archive(self, query: str, *,
                             conversation_id: ConversationId | None = None,
                             limit: int = 10) -> Sequence[ArchiveHit]:
        self._hold_lock()
        if conversation_id is not None and self._is_draft(conversation_id):
            return []
        scope = None if conversation_id is None else [conversation_id]
        return await self._memory.search(query, conversation_ids=scope, limit=limit)

    async def inspect_memory(self, conversation_id: ConversationId) -> MemoryView | None:
        self._hold_lock()
        if self._is_draft(conversation_id):
            return None
        return await self._memory.inspect(conversation_id)

    async def notes(self) -> Sequence[MemoryNote]:
        self._hold_lock()
        try:
            return await self._memory.notes(folder=self._project())
        except NotImplementedError:
            raise NotImplementedError(_NO_MEMORY_EDITING) from None

    # ── Control ──────────────────────────────────────────────────────────

    async def start(self, options: StartOptions) -> ConversationInfo:
        if self._closed:
            raise RuntimeStateError("the runtime is shut down")
        if self._state() not in (AgentState.NOT_STARTED, AgentState.IDLE, AgentState.STOPPED,
                                 AgentState.FAILED):
            raise RuntimeStateError(f"cannot start while {self._state()}")
        await self._withdraw_proposals()  # a proposal belongs to its conversation
        if self._ops:
            raise RuntimeStateError("operations are still outstanding")
        if options.conversation_id is not None and (options.seed_from is not None
                                                    or options.folder is not None):
            raise ValueError("seed_from and folder apply to a new conversation only")
        if options.folder is not None and not Path(options.folder).expanduser().is_dir():
            raise ValueError(f"{options.folder} is not a folder")
        self._hold_lock()
        if not self._models:
            await self._refresh_models()
        model = options.model or self._model or next(iter(self._models), None)
        if model is not None and model not in self._models:
            await self._refresh_models()  # one the provider added since: ask again
        if model is None:
            raise ValueError("Core lists no models")
        self._check_model(model)
        if (light := self._config.maintenance_model) is not None:
            self._check_model(light)

        if not self._started:
            data_dir = self._config.data_dir
            self._started = True
            await asyncio.to_thread(self._budget.load, data_dir / USAGE_FILE)
            self._budget.save()
            await self._budget.flush()
            self._log.open(data_dir / EVENT_LOG_FILE)
            self._loop_task = asyncio.create_task(self._run_loop())

        conversation, resumed = await self._open_conversation(options)
        if self._conversation is None or conversation.id != self._conversation.id:
            self._grants.clear()
        self._plan_call = None
        self._conversation = conversation
        self._draft = not resumed and options.seed_from is None
        self._model = model
        self._agent_config = AgentConfig(self._config.budget, self._permissions)
        # A resumed conversation has no pending wake-up: it waits for the user.
        self._wake_set, self._immediate_wake, self._unseen = None, False, False
        self._cancel_duty()
        self._answered()
        self._backlog.clear()
        if resumed:
            await self._repair()
        self._log.decision("start", conversation=conversation.id, resumed=resumed,
                           seed_from=conversation.seed_from, model=model)
        self._set_base(AgentState.IDLE)
        self._reminded = time.time()
        if conversation.duty is None:  # a duty waits for the user after it was closed
            self._schedule_duty()
        return conversation

    async def select_model(self, model_id: str) -> None:
        await self._refresh_models()
        self._check_model(model_id)
        self._model = model_id
        self._emit(ModelSelected(model_id))

    async def settings(self) -> Settings:
        cfg = self._config
        return Settings(
            model=self._model,
            maintenance_model=cfg.maintenance_model,
            budget=cfg.budget,
            permissions=self._permissions,
            system_prompt=cfg.system_prompt,
            **await self._modules.fields(),
        )

    async def update_settings(self, update: SettingsUpdate) -> Settings:
        async with self._settings_lock:
            return await self._update_settings(update)

    async def restart(self) -> None:
        if self.restart_request is not None:
            return
        conversation = self._conversation.id if self._conversation is not None else None
        self.restart_request = await self._modules.restart(conversation)
        self._log.decision("restart", frontend=self.restart_request.frontend,
                           conversation=self.restart_request.conversation_id)
        await self.aclose()

    async def send(self, text: str) -> None:
        state = self._state()
        if state == AgentState.NOT_STARTED:
            raise RuntimeStateError("not started")
        if state == AgentState.FAILED and (reason := self._budget.exhausted()):
            raise RuntimeStateError(f"budget exhausted: {reason}")
        queued = MessageQueued(_new_id(), text)
        self._inbox.append(queued)
        self._answered()
        if state in _RUNNING or state == AgentState.STOPPING:
            self._emit(queued)
        else:
            self._request_cycle("user message")

    async def activate(self) -> None:
        state = self._state()
        if state == AgentState.FAILED:
            if reason := self._budget.exhausted():
                raise RuntimeStateError(f"budget exhausted: {reason}")
        elif state not in (AgentState.IDLE, AgentState.STOPPED):
            raise RuntimeStateError(f"cannot activate while {state}")
        self._answered()
        self._request_cycle("user activation")

    async def stop(self) -> None:
        state = self._state()
        if state == AgentState.NOT_STARTED:
            return
        self._cancel_duty()  # a duty is paused until the user resumes it
        if state in (AgentState.STOPPING, AgentState.STOPPED):
            return
        if state in (AgentState.IDLE, AgentState.FAILED) and not self._ops:
            return
        self._stopping = True
        self._stop_event.set()
        self._inbox_before_stop = len(self._inbox)
        self._cycle_requested = False
        self._wake_set, self._immediate_wake = None, False
        self._log.decision("stop", state=state, operations=list(self._ops))
        for request_id, (request, future) in list(self._prompts.items()):
            del self._prompts[request_id]
            self._emit(PermissionResolved(request_id, Decision.DENY, request.call.id))
            future.set_result(Decision.DENY)
        for op in self._ops.values():
            op.request_cancel()
        self._update_state()
        self._stop_task = asyncio.create_task(self._finish_stop())

    async def cancel_operation(self, call_id: CallId) -> None:
        op = self._ops.get(call_id)
        if op is None:
            raise RuntimeStateError(f"call {call_id} is not running")
        self._log.decision("cancel_operation", call_id=call_id, by="user")
        op.request_cancel()

    # ── Conversations and long-term memory ───────────────────────────────

    async def rename_conversation(self, conversation_id: ConversationId, title: str) -> None:
        self._hold_lock()
        if self._is_draft(conversation_id):
            await self._save_draft()
        info = await self._memory.update_conversation(conversation_id, title=title)
        self._log.decision("rename_conversation", conversation=conversation_id,
                           cleared=info.title is None)
        self._conversation_updated(info)

    async def archive_conversation(self, conversation_id: ConversationId,
                                   archived: bool) -> None:
        self._hold_lock()
        if self._is_draft(conversation_id):
            await self._save_draft()
        info = await self._memory.update_conversation(conversation_id, archived=archived)
        self._log.decision("archive_conversation", conversation=conversation_id,
                           archived=archived)
        self._conversation_updated(info)

    async def delete_conversation(self, conversation_id: ConversationId) -> None:
        self._hold_lock()
        if self._conversation is not None and conversation_id == self._conversation.id:
            raise RuntimeStateError("this conversation is open; open another one first")
        try:
            await self._memory.delete_conversation(conversation_id)
        except NotImplementedError:
            raise NotImplementedError(_NO_DELETION) from None
        self._log.decision("delete_conversation", conversation=conversation_id)
        self._emit(ConversationDeleted(conversation_id))

    async def save_note(self, title: str, description: str, text: str, *,
                        note_id: str | None = None) -> MemoryNote:
        self._hold_lock()
        note = await self._edit_memories(
            self._memory.save_note(title, description, text, note_id=note_id,
                                   folder=self._project()))
        self._log.decision("note_saved", note=note.id, by="user")
        self._emit(MemoriesChanged())
        return note

    async def delete_note(self, note_id: str) -> None:
        self._hold_lock()
        await self._edit_memories(self._memory.delete_note(note_id, folder=self._project()))
        self._log.decision("note_deleted", note=note_id, by="user")
        self._emit(MemoriesChanged())

    async def skills(self) -> Sequence[Skill]:
        await self._load_skills()
        return tuple(s.skill for s in self._skills.values())

    async def _load_skills(self) -> None:
        """Read the skill folders again, so a new or changed skill needs no restart."""
        roots = skill_files.folders(self._config.data_dir, self._project())
        self._skills, problems = await asyncio.to_thread(skill_files.load, roots)
        if problems != self._skill_problems:
            self._skill_problems = problems
            for problem in problems:
                self._log.decision("skill_skipped", problem=problem)

    def _system(self) -> str:
        """The system prompt with the skills' listing."""
        section = skill_files.prompt(self._skills)
        return f"{self._config.system_prompt}\n\n{section}" if section else \
            self._config.system_prompt

    def _project(self) -> str | None:
        """Whose long-term memory: the open conversation's work folder (Memory.notes)."""
        return self._conversation.folder if self._conversation is not None else None

    async def _edit_memories[T](self, call: Awaitable[T]) -> T:
        try:
            return await call
        except NotImplementedError:
            raise NotImplementedError(_NO_MEMORY_EDITING) from None

    def _is_draft(self, conversation_id: ConversationId) -> bool:
        return (self._draft and self._conversation is not None
                and conversation_id == self._conversation.id)

    async def _save_draft(self) -> None:
        """Store the open conversation on its first use: a message, a duty or a name. Until
        then a new chat leaves nothing behind."""
        if not self._draft:
            return
        assert self._conversation is not None
        draft = self._conversation
        info = await self._memory.create_conversation(conversation_id=draft.id,
                                                      folder=draft.folder)
        self._draft = False
        self._log.decision("conversation_stored", conversation=info.id)
        self._conversation_updated(info)

    def _conversation_updated(self, info: ConversationInfo) -> None:
        if self._conversation is not None and info.id == self._conversation.id:
            self._conversation = info
        self._emit(ConversationUpdated(info))

    async def _find_conversation(self, conversation_id: ConversationId) -> ConversationInfo:
        for info in await self._memory.list_conversations(include_archived=True):
            if info.id == conversation_id:
                return info
        raise KeyError(conversation_id)

    async def set_duty(self, duty: Duty | None) -> None:
        if self._conversation is None:
            raise RuntimeStateError("no conversation is open")
        if duty is not None:
            duty = duties.check(duty)
        turned_on = duty is not None and self._conversation.duty is None
        await self._save_duty(duty, self._conversation.plan)
        self._answered()
        self._log.decision("duty", on=duty is not None,
                           kind=duty.kind if duty is not None else None)
        state = self._state()
        if duty is None:
            self._cancel_duty()
        elif turned_on and (state in (AgentState.IDLE, AgentState.STOPPED) or (
                state == AgentState.FAILED and not self._budget.exhausted())):
            self._request_cycle(f"duty: {duties.TURNED_ON}", duties.TURNED_ON)
        elif self._duty_timer is not None:
            self._schedule_duty()  # a changed interval applies at once

    async def set_plan(self, plan: Sequence[PlanItem]) -> None:
        if self._conversation is None:
            raise RuntimeStateError("no conversation is open")
        await self._save_duty(self._conversation.duty, plan)
        self._plan_call = None
        self._log.decision("plan", items=len(plan), by="user")
        if self._duty_timer is not None or self._conversation.duty is None:
            self._schedule_duty()

    async def _save_duty(self, duty: Duty | None, plan: Sequence[PlanItem]) -> None:
        assert self._conversation is not None
        await self._save_draft()
        try:
            info = await self._memory.save_duty(self._conversation.id, duty, tuple(plan))
        except NotImplementedError:
            raise NotImplementedError(_NO_DUTIES) from None
        self._conversation_updated(info)

    async def resolve_permission(self, request_id: str, decision: Decision) -> None:
        if self._state() != AgentState.AWAITING_PERMISSION:
            raise RuntimeStateError("no permission prompt is open")
        entry = self._prompts.pop(request_id, None)
        if entry is None:
            raise RuntimeStateError(f"unknown or already resolved request {request_id}")
        request, future = entry
        self._log.decision("permission", call_id=request.call.id, outcome=decision, by="user")
        self._emit(PermissionResolved(request_id, decision, request.call.id))
        future.set_result(decision)
        self._update_state()

    async def aclose(self) -> None:
        """Graceful shutdown: running calls end as by Stop (cancelled or abandoned), never
        interrupted. A second call waits for the first: a frontend may stop on ShuttingDown
        while Restart now is still closing modules and releasing the data directory."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.ensure_future(self._shut_down())
        await asyncio.shield(self._close_task)

    async def _shut_down(self) -> None:
        self._cycle_requested = False
        self._cancel_duty()
        await self.stop()
        if self._stop_task is not None:
            await self._stop_task
        if self._cycle_task is not None and not self._cycle_task.done():
            await asyncio.wait({self._cycle_task})
        if self._flush_tasks:
            await asyncio.wait(self._flush_tasks)
        if self._conversation is not None:
            with contextlib.suppress(_AppendFailed):
                await self._deliver_inbox()  # keep messages queued during a final Stop
        for task in (self._loop_task, self._duration_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._emit(ShuttingDown("shutdown"))
        for module in (self._core, self._memory, *self._tools):
            try:
                await module.aclose()
            except Exception:
                logger.exception("module aclose failed")
        if self._started:
            self._budget.save()
            await self._budget.flush()
        if self._lock is not None:
            self._lock.release()
        self._log.close()

    # ── State ────────────────────────────────────────────────────────────

    def _hold_lock(self) -> None:
        """Nothing reads or writes stored data before this process holds the data
        directory (InstanceLockedError otherwise): another process may have a conversation
        open."""
        if self._lock is None:
            self._lock = InstanceLock.acquire(self._config.data_dir)

    def _state(self) -> AgentState:
        if self._stopping:
            return AgentState.STOPPING
        if self._cycle_active or self._cycle_requested:
            return AgentState.AWAITING_PERMISSION if self._prompts else self._phase
        return self._base

    def _update_state(self) -> None:
        current = (self._state(), len(self._ops) - len(self._proposals))
        if current != self._emitted:
            self._emitted = current
            self._emit(StateChanged(current[0], current[1]))

    def _set_base(self, state: AgentState) -> None:
        self._base = state
        self._update_state()

    def _set_phase(self, phase: AgentState) -> None:
        self._phase = phase
        self._update_state()

    def _emit(self, event: RuntimeEvent) -> None:
        self._bus.emit(event)

    def _failure(self, kind: FailureKind, message: str) -> None:
        self._emit(Failure(kind, message))

    def _fail(self, kind: FailureKind, message: str) -> None:
        self._failure(kind, message)
        self._base = AgentState.FAILED
        self._cancel_duty()

    def _request_cycle(self, reason: str, duty_cause: str | None = None) -> None:
        if self._closed or self._stopping:
            return
        self._log.decision("cycle_requested", reason=reason)
        self._cancel_duty()
        self._duty_cause = duty_cause
        self._cycle_requested = True
        self._wake_set, self._immediate_wake = None, False
        self._phase = AgentState.THINKING
        self._wake.set()
        self._update_state()

    # ── Settings ─────────────────────────────────────────────────────────

    async def _update_settings(self, u: SettingsUpdate) -> Settings:
        changed = [f.name for f in fields(u) if getattr(u, f.name) is not None]
        if not changed:
            return await self.settings()
        cfg = self._config
        saved = await self._modules.prepare(u)
        # Models belong to the running Core; with another one chosen, they are not saved.
        core_pending = ("core" in saved.get("modules", {})
                        or self._modules.chosen["core"] != self._modules.running["core"])
        if u.system_prompt is not None and not u.system_prompt.strip():
            raise ValueError("the system prompt is empty")
        budget = cfg.budget if u.budget is None else u.budget
        model = self._model if u.model is None else u.model
        light = cfg.maintenance_model
        if u.maintenance_model is not None:
            light = u.maintenance_model or None
        models = self._models
        if not models and (model or light):  # before start(): only to check them
            models = {m.id: m for m in await self._core.list_models()}
        for m in (model, light):
            if m is not None:
                self._check_model(m, models, budget)

        if not core_pending and u.model is not None and model != self._model:
            saved["model"] = model
        if not core_pending and light != cfg.maintenance_model:
            saved["maintenance_model"] = light or ""
        if u.budget is not None:
            saved["budget"] = {k: v for k, v in asdict(budget).items() if v is not None}
        if u.permissions is not None:
            saved["permissions"] = u.permissions.value
        if u.system_prompt is not None:
            saved["system_prompt"] = u.system_prompt
        await self._modules.commit(u, saved)

        # Apply: nothing below can fail.
        loosened = self._loosened(u)
        self._models = models
        permissions = self._permissions
        if u.permissions is not None:
            permissions = _effective_policy(u.permissions, self._ceiling)
            os.environ[PERMISSION_CEILING_ENV] = permissions.value
        self._permissions = permissions
        self._budget.budget = budget
        self._config = replace(
            cfg, maintenance_model=light, budget=budget,
            permissions=u.permissions or cfg.permissions,
            system_prompt=u.system_prompt or cfg.system_prompt)
        if self._agent_config is not None:
            self._agent_config = replace(self._agent_config, budget=budget,
                                         permissions=permissions)
        model_changed = model != self._model
        self._model = model
        self._log.decision(
            "settings", changed=changed, loosened=loosened,
            **{k: v for k, v in saved.items() if k != "system_prompt"},
            **({"system_prompt_chars": len(u.system_prompt)} if u.system_prompt else {}))
        if model_changed and model is not None:
            self._emit(ModelSelected(model))
        settings = await self.settings()
        self._emit(SettingsChanged(settings))
        self._watch_duration()
        return settings

    def _loosened(self, u: SettingsUpdate) -> list[str]:
        """Changes a frontend must have confirmed with the user."""
        loosened = []
        old = self._config.budget
        if u.budget is not None and any(
                getattr(old, f.name) is not None and (
                    getattr(u.budget, f.name) is None
                    or getattr(u.budget, f.name) > getattr(old, f.name))
                for f in fields(Budget)):
            loosened.append("budget")
        if u.permissions is not None:
            order = list(PermissionPolicy)
            if order.index(u.permissions) > order.index(self._config.permissions):
                loosened.append("permissions")
        return loosened

    # ── Models and context sizing ────────────────────────────────────────

    async def _refresh_models(self) -> None:
        self._models = {m.id: m for m in await self._core.list_models()}

    def _check_model(self, model_id: str, models: dict[str, ModelInfo] | None = None,
                     budget: Budget | None = None) -> None:
        info = (self._models if models is None else models).get(model_id)
        if info is None:
            raise ValueError(f"unknown model {model_id!r}")
        if info.context_window is None:
            raise ValueError(f"model {model_id!r} has no known context window; configure "
                             "one in the Core's models option")
        budget = self._config.budget if budget is None else budget
        if budget.max_cost_usd is not None and not has_prices(info):
            raise ValueError(f"a cost budget needs prices for model {model_id!r}")

    def _info(self) -> ModelInfo:
        assert self._model is not None
        return self._models[self._model]

    def _maintenance_info(self) -> ModelInfo:
        light = self._config.maintenance_model
        return self._models[light] if light else self._info()

    def _window(self, info: ModelInfo) -> int:
        assert info.context_window is not None  # checked by _check_model
        return info.context_window

    def _counter(self, model: str) -> TokenCounter:
        return calibrated(self._calibration.get(model, 1.0))

    def _context_request(self, info: ModelInfo, tools: Sequence[ToolSpec],
                         factor: float = 1.0) -> tuple[ContextRequest, int]:
        """The assembled context gets the window minus system prompt, tools and output."""
        assert self._conversation is not None
        window = self._window(info)
        reserve = min(info.max_output_tokens or self._config.default_max_output_tokens,
                      window // 4)
        count = self._counter(info.id)
        tool_json = json.dumps([[t.name, t.description, t.parameters] for t in tools])
        budget = window - reserve - count(self._system()) - count(tool_json)
        return ContextRequest(self._conversation.id, max(0, int(budget * factor)), count), reserve

    # ── Tools offered to the model ───────────────────────────────────────

    async def _offered_tools(self) -> dict[str, ToolSpec]:
        """The runtime's built-ins, then Memory's tools, then the Core's (its provider runs
        them), then each Tools module's; a name offered already is dropped from the later
        module."""
        specs = dict(BUILTIN_TOOLS)
        conversation = self._conversation
        if conversation is not None:
            specs["update_plan"] = DUTY_TOOLS["update_plan"]
            if conversation.duty is not None:
                specs["ask_for_help"] = DUTY_TOOLS["ask_for_help"]
            if conversation.duty is not None and conversation.duty.kind == DutyKind.GOAL:
                specs["finish_goal"] = DUTY_TOOLS["finish_goal"]
        await self._load_skills()
        if self._skills:
            specs[SKILL_TOOL.name] = SKILL_TOOL
        self._memory_tools, self._tool_owner = set(), {}
        for module in (self._memory, self._core, *self._tools):
            try:
                offered = await module.list_tools()
            except Exception as e:  # noqa: BLE001 - module calls are wrapped
                self._failure(FailureKind.INTERNAL,
                              f"{type(module).__name__}.list_tools failed: {e}")
                offered = []
            for spec in offered:
                if spec.name in specs:
                    self._log.decision("duplicate_tool_dropped", name=spec.name,
                                       module=type(module).__name__)
                    continue
                specs[spec.name] = spec
                if module is self._memory:
                    self._memory_tools.add(spec.name)
                elif module is not self._core:
                    self._tool_owner[spec.name] = module
        return {name: self._describable(spec) for name, spec in specs.items()}

    def _describable(self, spec: ToolSpec) -> ToolSpec:
        """The spec with the optional description (api.DESCRIPTION_ARG) added."""
        properties = spec.parameters.get("properties", {})
        if DESCRIPTION_ARG in properties:
            self._own_description.add(spec.name)
            return spec
        self._own_description.discard(spec.name)
        return replace(spec, parameters={
            **spec.parameters, "properties": {**properties, DESCRIPTION_ARG: _DESCRIPTION_SCHEMA}})

    def _bare(self, call: ToolCall) -> ToolCall:
        """The call as the Tools module gets it: without the description the runtime added."""
        if call.name in self._own_description or DESCRIPTION_ARG not in call.arguments:
            return call
        return replace(call, arguments={k: v for k, v in call.arguments.items()
                                        if k != DESCRIPTION_ARG})

    # ── Conversations and resume repair ──────────────────────────────────

    async def _open_conversation(self, options: StartOptions) -> tuple[ConversationInfo, bool]:
        """Find or create the conversation and move the Tools to its folder."""
        if options.conversation_id is not None:
            info = await self._find_conversation(options.conversation_id)
            await self._use_folder(info.folder)
            return info, True
        folder = options.folder
        if folder is None and options.seed_from is not None:
            folder = (await self._find_conversation(options.seed_from)).folder
        folder = folder or self._modules.work_folder
        if folder is not None:
            folder = str(Path(folder).expanduser().resolve())
        if not await self._use_folder(folder):
            if options.folder is not None:
                raise ValueError("these tools work in one fixed folder")
            folder = None
        if options.seed_from is None:  # stored with its first use (_save_draft)
            now = time.time()
            return ConversationInfo(_new_id(), None, now, now, folder=folder), False
        try:
            info = await self._memory.create_conversation(seed_from=options.seed_from,
                                                          folder=folder)
        except BaseException:
            if self._conversation is not None:
                with contextlib.suppress(ValueError):
                    await self._use_folder(self._conversation.folder)
            raise
        return info, False

    async def _use_folder(self, folder: str | None) -> bool:
        """Hand every Tools module that takes one the folder; False when none does."""
        taken = False
        for module in self._tools:
            try:
                await module.use_folder(None if folder is None else Path(folder))
            except NotImplementedError:
                continue
            except OSError as e:
                raise ValueError(
                    f"the folder {folder} cannot be used: {e.strerror or e}") from e
            taken = True
        return taken

    async def _repair(self) -> None:
        assert self._conversation is not None
        history = await self._memory.history(self._conversation.id)
        answered, backgrounded, completed = set(), [], set()
        calls: list[ToolCall] = []
        for m in history:
            for b in m.blocks:
                match b:
                    case ToolCall():
                        calls.append(b)
                    case ToolResult():
                        answered.add(b.call_id)
                        if b.status == ToolStatus.BACKGROUNDED:
                            backgrounded.append(b.call_id)
                    case OperationResult():
                        completed.add(b.call_id)
        now = time.time()
        missing = [c.id for c in calls if c.id not in answered]
        if missing:
            note = "Interrupted: the process ended while this call was in flight."
            await self._append(Message(_new_id(), Role.TOOL, tuple(
                ToolResult(cid, ToolStatus.INTERRUPTED, note) for cid in missing), now))
        orphans = [cid for cid in backgrounded if cid not in completed]
        if orphans:
            note = "Interrupted: the process ended while this operation was running."
            await self._append(Message(_new_id(), Role.USER, tuple(
                OperationResult(cid, ToolStatus.INTERRUPTED, note) for cid in orphans), now))
        if missing or orphans:
            self._log.decision("resume_repair", interrupted_calls=missing,
                               interrupted_operations=orphans)

    # ── Transcript writes ────────────────────────────────────────────────

    async def _append(self, message: Message | None = None,
                      events: Sequence[RuntimeEvent] = (), stop: StopReason | None = None
                      ) -> None:
        """Append in order behind any backlog; on failure the message stays queued and
        nothing further starts until an append succeeds."""
        assert self._conversation is not None
        async with self._mem_lock:
            if message is not None:
                self._backlog.append((message, list(events), stop))
            while self._backlog:
                msg, after, reason = self._backlog[0]
                try:
                    await self._save_draft()
                    await self._memory.append(self._conversation.id, [msg])
                except Exception as e:
                    self._failure(FailureKind.INTERNAL, f"Memory.append failed: {e}")
                    raise _AppendFailed from e
                self._backlog.popleft()
                self._emit(MessageCommitted(msg, reason))
                for event in after:
                    self._emit(event)

    async def _flush_results(self) -> None:
        """Append terminated background outcomes as one USER message."""
        if not self._pending_results:
            return
        pending, self._pending_results = self._pending_results, []
        message = Message(_new_id(), Role.USER, tuple(r for r, _ in pending), time.time())
        self._unseen = True
        await self._append(message, [finished for _, finished in pending])

    async def _deliver_inbox(self) -> bool:
        if not self._inbox:
            return False
        if any(p.card for p, _ in self._proposals.values()):
            # The user wrote instead of clicking: its outcome goes before their message.
            await self._withdraw_proposals(cards_only=True)
            if self._cycle_active:
                await self._flush_results()
        while self._inbox:
            q = self._inbox.pop(0)
            await self._append(Message(q.message_id, Role.USER, (Text(q.text),), time.time()))
        return True

    async def _ensure_user_last(self) -> None:
        """A cycle without a new user message starts with a runtime notice when the
        transcript is empty or ends on an assistant message."""
        assert self._conversation is not None
        newest = [] if self._draft else await self._memory.history(self._conversation.id,
                                                                    limit=1)
        if not newest or newest[-1].role == Role.ASSISTANT:
            self._log.decision("activation_notice")
            await self._append(
                Message(_new_id(), Role.USER, (Text(ACTIVATION_NOTICE),), time.time()))

    async def _flush_outside_cycle(self) -> None:
        try:
            await self._flush_results()
        except _AppendFailed:
            if not self._cycle_active and not self._stopping:
                self._set_base(AgentState.FAILED)
            return
        self._check_wake()

    # ── Cycle loop ───────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if not self._cycle_requested or self._stopping or self._closed:
                continue
            if self._flush_tasks:
                await asyncio.wait(self._flush_tasks)
            self._cycle_requested = False
            self._cycle_active = True
            self._cycle_task = asyncio.create_task(self._cycle())
            await asyncio.wait({self._cycle_task})

    async def _cycle(self) -> None:
        self._log.decision("cycle_start")
        try:
            await self._cycle_body()
        except _AppendFailed:
            self._base = AgentState.FAILED
        except Exception as e:
            logger.exception("cycle failed")
            self._fail(FailureKind.INTERNAL, f"Runtime error: {type(e).__name__}: {e}")
        finally:
            self._cycle_active = False
            self._phase = AgentState.THINKING
            self._log.decision("cycle_end", state=self._base)
            if not self._stopping:
                await self._flush_outside_cycle()
            if self._waiting():
                self._schedule_duty()
            self._update_state()

    async def _cycle_body(self) -> None:
        self._set_phase(AgentState.THINKING)
        await self._append()  # drain any backlog first
        await self._flush_results()
        await self._deliver_inbox()
        if self._duty_cause is not None:
            cause, self._duty_cause = self._duty_cause, None
            duty = self._conversation.duty if self._conversation is not None else None
            notice = duties.notice(cause, duty)
            if duty is None and cause == duties.DUE and self._conversation is not None:
                self._reminded = time.time()
                notice = duties.reminder(self._conversation.plan, self._reminded)
            await self._append(Message(_new_id(), Role.USER, (Text(notice),), time.time()))
        else:
            await self._ensure_user_last()
        while True:
            if self._stopping:
                return
            self._set_phase(AgentState.THINKING)
            specs = await self._offered_tools()
            await self._maintain(list(specs.values()))
            if self._stopping:
                return
            if reason := self._check_budget("inference"):
                self._fail(FailureKind.BUDGET, f"Budget exhausted: {reason}")
                return
            completed = await self._infer(specs)
            if completed is None:
                return
            assistant = completed.message
            calls = [b for b in assistant.blocks if isinstance(b, ToolCall)]
            try:
                await self._append(assistant, stop=completed.stop)
            except _AppendFailed:
                if calls:  # keep the transcript consistent when the backlog is replayed
                    self._backlog.append((Message(_new_id(), Role.TOOL, tuple(
                        ToolResult(c.id, ToolStatus.ERROR,
                                   "Not run: the assistant message could not be saved.")
                        for c in calls), time.time()), [], None))
                raise
            if calls:
                self._set_phase(AgentState.RUNNING_TOOLS)
                results, finished = await self._run_tools(calls, specs)
                await self._append(
                    Message(_new_id(), Role.TOOL, tuple(results), time.time()), finished)
            if self._stopping:
                return
            await self._flush_results()
            delivered = await self._deliver_inbox()
            ended = (len(calls) == 1 and calls[0].name == "wrap_up"
                     and results[0].status in (ToolStatus.OK, ToolStatus.BACKGROUNDED))
            if (calls and not ended) or delivered:  # a queued message authorizes the next turn
                continue
            # After the answer: Memory saves what the exchange taught it.
            before = await self._note_versions()
            if await self._maintain(list(specs.values())):
                after = await self._note_versions()
                saved = tuple(title for key, title in after.items() if key not in before)
                self._emit(MemoriesChanged(saved))
            if self._stopping:
                return
            self._enter_idle()
            return

    def _enter_idle(self) -> None:
        self._base = AgentState.IDLE
        # Woken once everything running now has finished; a proposal wakes it by itself.
        wake = set(self._ops) - self._proposal_ops
        self._wake_set = wake or None
        self._immediate_wake = not wake and self._unseen
        self._log.decision("idle", wait_for=sorted(wake), immediate=self._immediate_wake)

    def _waiting(self) -> bool:
        return (self._base == AgentState.IDLE and not self._cycle_active
                and not self._cycle_requested and not self._stopping and not self._closed)

    def _check_wake(self) -> None:
        if not self._waiting():
            return
        if self._immediate_wake:
            self._request_cycle("wake-up")
        elif self._proposal_done:
            self._request_cycle("proposal resolved")
        elif self._wake_set:
            running = self._wake_set & set(self._ops)
            if self._duty_running() and running != self._wake_set:
                self._request_cycle(f"duty: {duties.FINISHED}", duties.FINISHED)
            elif not running:
                self._request_cycle("wake-up")

    # ── Duties ───────────────────────────────────────────────────────────

    def _duty_running(self) -> bool:
        return self._conversation is not None and self._conversation.duty is not None

    def _schedule_duty(self) -> None:
        """While idle, wake the duty at its next time (duties.next_wake); without a duty,
        when the next step falls due (duties.next_reminder)."""
        self._cancel_duty()
        if self._conversation is None or not self._waiting():
            return
        duty = self._conversation.duty
        if duty is None:
            due = duties.next_reminder(self._conversation.plan, self._reminded)
            if due is not None:
                self._wake_in(max(self._config.reminder_min_s, due - time.time()), duties.DUE)
            return
        if self._help is not None:
            self._log.decision("duty_paused", reason="waiting for help")
            return
        self._wake_in(*duties.next_wake(duty, self._conversation.plan))

    def _wake_in(self, delay: float, cause: str) -> None:
        self._duty_timer = asyncio.create_task(self._duty_due(delay, cause))
        self._duty_wake_at = time.time() + delay
        self._log.decision("duty_scheduled", in_s=round(delay, 3), cause=cause)
        self._emit(DutyScheduled(self._duty_wake_at))

    def _answered(self) -> None:
        if self._help is not None:
            self._help = None
            self._emit(HelpNeeded(None))

    def _cancel_duty(self) -> None:
        if self._duty_timer is not None:
            self._duty_timer.cancel()
            self._duty_timer = None
        if self._duty_wake_at is not None:
            self._duty_wake_at = None
            self._emit(DutyScheduled(None))

    async def _duty_due(self, delay: float, cause: str) -> None:
        await asyncio.sleep(delay)
        self._duty_timer = None
        if self._waiting():
            self._request_cycle(f"duty: {cause}", cause)

    # ── Budgets ──────────────────────────────────────────────────────────

    def _check_budget(self, before: str) -> str | None:
        reason = self._budget.exhausted()
        self._log.decision("budget_check", before=before, ok=reason is None, reason=reason)
        return reason

    def _watch_duration(self) -> None:
        if self._config.budget.max_duration_s is not None and (
                self._duration_task is None or self._duration_task.done()):
            self._duration_task = asyncio.create_task(self._duration_watchdog())

    async def _duration_watchdog(self) -> None:
        """While operations run, cancel them all as Stop does once the duration budget is
        exhausted. Inference has its own deadline (_race_stop_or_deadline)."""
        while self._ops:
            remaining = self._budget.seconds_until_duration_exhausted()
            if remaining:
                await asyncio.sleep(remaining)
                continue
            self._log.decision("duration_exhausted", operations=list(self._ops))
            for op in self._ops.values():
                op.request_cancel()
            if (self._base == AgentState.IDLE and not self._cycle_active
                    and not self._cycle_requested and not self._stopping):
                self._fail(FailureKind.BUDGET, f"Budget exhausted: {self._budget.exhausted()}")
                self._update_state()
            return

    def _charge(self, usage: Usage, info: ModelInfo) -> None:
        self._budget.charge(usage, cost_of(usage, info))
        self._emit(UsageUpdated(self._budget.snapshot()))

    # ── Maintenance ──────────────────────────────────────────────────────

    async def _note_versions(self) -> dict[tuple[str, float], str]:
        """Each note's (id, last written) -> title; empty when Memory keeps no notes."""
        try:
            return {(n.id, n.updated_at): n.title
                    for n in await self._memory.notes(folder=self._project())}
        except NotImplementedError:
            return {}
        except Exception:  # module calls are wrapped; no notice is no harm
            logger.exception("Memory.notes failed")
            return {}

    async def _maintain(self, tools: Sequence[ToolSpec]) -> bool:
        """Whether Memory acted."""
        assert self._conversation is not None
        context, _ = self._context_request(self._info(), tools)
        complete = _Completion(self)
        async with self._mem_lock:
            task = asyncio.ensure_future(
                self._memory.maintain(self._conversation.id, complete, context))
            if await self._race_stop(task):
                return False
        if complete.started:
            self._emit(MaintenanceFinished(self._conversation.id))
        exc = None if task.cancelled() else task.exception()
        if complete.compacting is not None:
            compacted = (complete.compacting and not task.cancelled() and exc is None
                         and bool(task.result()))
            self._emit(CompactionFinished(self._conversation.id, compacted))
        if task.cancelled():
            return False
        if exc is None and task.result() and self._conversation.title is None:
            info = await self._find_conversation(self._conversation.id)
            if info.title is not None:  # Memory wrote an automatic title
                self._log.decision("title", conversation=info.id)
                self._conversation_updated(info)
        if exc is None:
            return bool(task.result())
        if not isinstance(exc, BudgetExhausted):  # the budget is re-checked by the caller
            kind = FailureKind.MODEL if isinstance(exc, CoreError) else FailureKind.INTERNAL
            self._failure(kind, f"Memory maintenance failed: {exc}")
        return False

    async def _race_stop(self, task: asyncio.Future[Any]) -> bool:
        """Wait for task or Stop. On Stop, cancel it and allow the grace period.
        Returns True if Stop interrupted the task."""
        stopper = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            stopper.cancel()
        if task.done():
            return False
        task.cancel()
        await asyncio.wait({task}, timeout=self._config.cancel_grace_s)
        if not task.done():
            self._failure(FailureKind.INTERNAL,
                          "A module call did not confirm cancellation; it was abandoned.")
        return True

    # ── Inference ────────────────────────────────────────────────────────

    def _backoff(self, attempt: int, error: TransientCoreError) -> float:
        if error.retry_after is not None:
            return error.retry_after
        return min(self._config.retry_base_delay_s * 2 ** (attempt - 1),
                   self._config.retry_max_delay_s)

    async def _build_request(self, specs: dict[str, ToolSpec], factor: float
                             ) -> InferenceRequest:
        info = self._info()
        tools = list(specs.values())
        context, reserve = self._context_request(info, tools, factor)
        assembled = await self._memory.assemble(context)
        system = self._system()
        if assembled.system_addendum:
            system = f"{system}\n\n{assembled.system_addendum}"
        assert self._conversation is not None
        in_view = self._plan_call is not None and any(
            isinstance(b, ToolCall) and b.id == self._plan_call
            for m in assembled.messages for b in m.blocks)
        if section := duties.prompt(self._conversation.duty, self._conversation.plan,
                                    in_view=in_view):
            system = f"{system}\n\n{section}"
        self._unseen = self._proposal_done = False
        messages = [_stamped(m) for m in assembled.messages]
        return InferenceRequest(info.id, system, messages, tools, reserve)

    async def _calibrate(self, request: InferenceRequest) -> None:
        if request.model in self._calibration:
            return
        self._calibration[request.model] = 1.0
        try:
            exact = await self._core.count_tokens(request)
        except Exception:  # noqa: BLE001 - calibration is optional
            return
        estimate = self._request_estimate(request, estimate_tokens)
        if exact and estimate:
            self._calibration[request.model] = min(4.0, max(0.25, exact / estimate))

    def _request_estimate(self, request: InferenceRequest, count: TokenCounter) -> int:
        tool_json = json.dumps([[t.name, t.description, t.parameters] for t in request.tools])
        return count(request.system) + count(request.messages) + count(tool_json)

    async def _infer(self, specs: dict[str, ToolSpec]) -> Completed | None:
        """Run one model turn with retries. Returns None when the cycle must end."""
        attempts, factor, overflowed = 0, 1.0, False
        max_attempts = self._config.max_retries + 1
        while True:
            request = await self._build_request(specs, factor)
            await self._calibrate(request)
            attempts += 1
            try:
                return await self._attempt(request, self._models[request.model],
                                           foreground=True)
            except _Stopped:
                return None
            except _BudgetHit as e:
                self._fail(FailureKind.BUDGET, f"Budget exhausted mid-stream: {e}")
                return None
            except ContextOverflowError as e:
                if overflowed:
                    self._fail(FailureKind.MODEL, f"Context overflow after retry: {e}")
                    return None
                overflowed, factor = True, factor * 0.9
                self._emit(ModelRetry(attempts, max_attempts, 0.0, str(e)))
            except TransientCoreError as e:
                if attempts >= max_attempts:
                    self._fail(FailureKind.MODEL, f"Model failed after {attempts} attempts: {e}")
                    return None
                delay = self._backoff(attempts, e)
                self._emit(ModelRetry(attempts, max_attempts, delay, str(e)))
                sleeper = asyncio.ensure_future(asyncio.sleep(delay))
                if await self._race_stop(sleeper):
                    return None
            except CoreError as e:
                self._fail(FailureKind.MODEL, f"Model error: {e}")
                return None
            except Exception as e:  # noqa: BLE001 - unexpected Core exceptions are permanent
                self._fail(FailureKind.MODEL, f"Model error: {type(e).__name__}: {e}")
                return None

    async def _attempt(self, request: InferenceRequest, info: ModelInfo, *,
                       foreground: bool) -> Completed:
        """One streamed request. Charges usage whatever the outcome."""
        stream = _Stream(self._request_estimate(request, self._counter(request.model)))
        self._budget.begin_activity()
        task = asyncio.ensure_future(self._consume(request, stream, info, foreground))
        try:
            if foreground:
                stopped = await self._race_stop_or_deadline(task)
            else:
                await asyncio.wait({task})
                stopped = False
        except asyncio.CancelledError:
            task.cancel()
            self._charge(stream.estimate(), info)
            raise
        finally:
            self._budget.end_activity()
        if stopped:
            self._charge(stream.estimate(), info)
            await self._commit_partial(stream)
            raise _Stopped
        if not task.done():  # duration budget ran out while streaming
            task.cancel()
            await asyncio.wait({task}, timeout=self._config.cancel_grace_s)
            self._charge(stream.estimate(), info)
            await self._commit_partial(stream)
            raise _BudgetHit(self._budget.exhausted() or "max_duration_s exhausted")
        exc = None if task.cancelled() else task.exception()
        if stream.completed is not None and exc is None:
            self._charge(stream.completed.usage, info)
            return stream.completed
        if stream.started or exc is None:  # a failure before the first event is unbilled
            self._charge(stream.estimate(), info)
        if isinstance(exc, _BudgetHit):
            await self._commit_partial(stream)
        if exc is None:
            raise CoreError("inference was cancelled")
        raise exc

    async def _race_stop_or_deadline(self, task: asyncio.Future[Any]) -> bool:
        stopper = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED,
                               timeout=self._budget.seconds_until_duration_exhausted())
        finally:
            stopper.cancel()
        if task.done() or not self._stop_event.is_set():
            return False
        task.cancel()
        await asyncio.wait({task}, timeout=self._config.cancel_grace_s)
        if not task.done():
            self._failure(FailureKind.INTERNAL, "Inference did not confirm cancellation.")
        return True

    async def _consume(self, request: InferenceRequest, st: _Stream, info: ModelInfo,
                       foreground: bool) -> None:
        count = self._counter(request.model)
        async with contextlib.aclosing(self._core.stream(request)) as events:
            async for event in events:
                st.started = True
                match event:
                    case Completed():
                        st.completed = event
                        return
                    case UsageReport(usage=usage):
                        st.report = usage
                    case TextDelta(text=t):
                        st.text.append(t)
                        st.output_estimate += count(t)
                    case ReasoningDelta(text=t):
                        st.output_estimate += count(t)
                    case ToolCallDelta(arguments_chunk=chunk, name=name):
                        st.output_estimate += count(chunk + (name or ""))
                if foreground and not isinstance(event, UsageReport):
                    self._emit(event)
                in_flight = st.estimate()
                if reason := self._budget.exhausted(in_flight, cost_of(in_flight, info)):
                    raise _BudgetHit(reason)
        raise CoreError("stream ended without Completed")

    async def _commit_partial(self, st: _Stream) -> None:
        """Commit streamed assistant text (text only) when inference is cut short."""
        text = "".join(st.text)
        if text:
            await self._append(Message(_new_id(), Role.ASSISTANT, (Text(text),), time.time()))

    # ── Tool calls ───────────────────────────────────────────────────────

    def _validate(self, call: ToolCall, specs: dict[str, ToolSpec]) -> str | None:
        if call.raw_arguments is not None:
            return f"Arguments for {call.name!r} are not valid JSON: {call.raw_arguments[:200]}"
        spec = specs.get(call.name)
        if spec is None:
            return f"Unknown tool {call.name!r}."
        try:
            validator = jsonschema.validators.validator_for(spec.parameters)(spec.parameters)
            error = jsonschema.exceptions.best_match(validator.iter_errors(call.arguments))
        except jsonschema.exceptions.SchemaError as e:
            return f"Tool {call.name!r} has an invalid parameter schema: {e.message}"
        if error is not None:
            return f"Invalid arguments for {call.name!r}: {error.message}"
        return None

    async def _run_tools(self, calls: list[ToolCall], specs: dict[str, ToolSpec]
                         ) -> tuple[list[ToolResult], list[RuntimeEvent]]:
        """Consecutive read-only calls run concurrently; a non-read-only call starts once
        every earlier call has finished or been backgrounded, and later calls wait for it.
        Returns results in emission order and the ToolFinished events to emit after the
        TOOL message is appended."""
        self._specs = specs
        results: dict[CallId, tuple[ToolResult, float]] = {}
        tasks: dict[CallId, asyncio.Task[tuple[ToolResult, float]]] = {}
        barrier: asyncio.Task[Any] | None = None
        for call in calls:
            if call.name == "wrap_up" and len(calls) > 1:
                results[call.id] = (ToolResult(call.id, ToolStatus.ERROR, (
                    "Not shown: call wrap_up alone, in a message of its own after your other "
                    "work.")), 0.0)
                continue
            if error := self._validate(call, specs):
                results[call.id] = (ToolResult(call.id, ToolStatus.ERROR, error), 0.0)
                continue
            spec = specs[call.name]
            if spec.read_only:
                after = [barrier] if barrier is not None else []
            else:
                after = list(tasks.values())
            task = asyncio.create_task(self._run_call(call, spec, after))
            tasks[call.id] = task
            if not spec.read_only:
                barrier = task
        if tasks:
            await asyncio.wait(tasks.values())
        for call_id, task in tasks.items():
            results[call_id] = task.result()
        ordered = [results[c.id] for c in calls]
        finished: list[RuntimeEvent] = [ToolFinished(r, d) for r, d in ordered
                                        if r.status != ToolStatus.BACKGROUNDED]
        return [r for r, _ in ordered], finished

    async def _run_call(self, call: ToolCall, spec: ToolSpec, after: list[asyncio.Task[Any]]
                        ) -> tuple[ToolResult, float]:
        if after:
            await asyncio.wait(after)
        try:
            if call.name == "propose":
                return self._propose(call), 0.0
            if call.name == "wrap_up":
                return self._wrap_up(call), 0.0
            if call.name in _RUNTIME_TOOLS or call.name in self._memory_tools:
                return await self._run_builtin(call, spec)
            return await self._run_module_call(call, spec)
        except Exception as e:
            logger.exception("tool call handling failed")
            return ToolResult(call.id, ToolStatus.ERROR, f"Runtime error: {e}"), 0.0

    async def _run_module_call(self, call: ToolCall, spec: ToolSpec
                               ) -> tuple[ToolResult, float]:
        not_started = ToolResult(call.id, ToolStatus.CANCELLED,
                                 "Not started: the agent was stopped.")
        if self._stopping:
            return not_started, 0.0
        if refusal := await self._gate(call, spec):
            return ToolResult(call.id, ToolStatus.REFUSED, refusal), 0.0
        if self._stopping:
            return not_started, 0.0
        reason = self._budget.reserve_tool_call()
        self._log.decision("budget_check", before="tool", call_id=call.id, ok=reason is None,
                           reason=reason)
        if reason:
            return ToolResult(call.id, ToolStatus.REFUSED, f"Refused: budget {reason}."), 0.0
        self._emit(UsageUpdated(self._budget.snapshot()))
        op = self._launch(call, spec)
        assert op.supervisor is not None
        await asyncio.wait({op.supervisor}, timeout=self._config.initial_wait_s)
        if not op.supervisor.done() and (self._stopping or op.cancel_requested):
            await asyncio.wait({op.supervisor})
        if op.supervisor.done():
            return op.supervisor.result(), op.duration
        op.backgrounded = True
        self._log.decision("backgrounded", call_id=call.id)
        self._emit(ToolBackgrounded(call.id))
        progress = f" Last progress: {op.last_progress}." if op.last_progress else ""
        return ToolResult(
            call.id,
            ToolStatus.BACKGROUNDED,
            f"Operation {call.id} is still running in the background "
            f"({op.duration:.1f}s so far).{progress} Use op_status to check on it or wait, "
            "op_cancel to cancel it; its outcome is delivered when it finishes.",
        ), op.duration

    async def _gate(self, call: ToolCall, spec: ToolSpec) -> str | None:
        """Apply the permission policy. Returns a refusal message, or None to run."""
        policy = self._permissions

        def auto(decision: Decision, why: str) -> None:
            self._log.decision("permission", call_id=call.id, outcome=decision, by=why)
            self._emit(PermissionResolved(_new_id(), decision, call.id))

        if policy == PermissionPolicy.DENY:
            auto(Decision.DENY, "policy")
            return "Refused: the permission policy denies tool calls."
        try:
            owner = self._tool_owner.get(call.name)
            confined = owner is not None and await owner.confined(self._bare(call))
        except Exception:  # module calls are wrapped; not knowing means asking
            logger.exception("Tools.confined failed for %s", call.name)
            confined = False
        if call.name in self._grants and (confined or self._grants[call.name]):
            auto(Decision.ALLOW_CONVERSATION, "grant")
            return None
        if policy == PermissionPolicy.ALLOW_READ_ONLY and spec.read_only:
            auto(Decision.ALLOW_ONCE, "policy")
            return None
        if policy in (PermissionPolicy.ALLOW_WORKSPACE, PermissionPolicy.AUTO) and confined:
            auto(Decision.ALLOW_ONCE, "policy")
            return None
        reason = None
        if policy == PermissionPolicy.AUTO:
            verdict = await self._review(call, spec)
            if self._stopping:
                return None  # the caller reports the call as not started
            if verdict.allow:
                auto(Decision.ALLOW_ONCE, "review")
                return None
            reason = verdict.reason
        if not self._frontend.interactive_permissions:
            auto(Decision.DENY, "non-interactive frontend")
            return "Refused: this call needs permission and the frontend cannot ask."
        request = PermissionRequest(_new_id(), call, spec.read_only, confined, reason)
        future: asyncio.Future[Decision] = asyncio.get_running_loop().create_future()
        self._prompts[request.id] = (request, future)
        self._emit(PermissionRequested(request))
        self._update_state()
        decision = await future
        self._update_state()
        if decision == Decision.DENY:
            return "Refused: permission denied."
        if decision == Decision.ALLOW_CONVERSATION:
            self._grants[call.name] = self._grants.get(call.name, False) or not confined
        return None

    async def _review(self, call: ToolCall, spec: ToolSpec) -> Verdict:
        """The automatic permission check (review.py). Fails closed: any problem asks."""
        if reason := always_ask(call, self._config.data_dir):
            self._log.decision("permission_review", call_id=call.id, allow=False, by="rule")
            return Verdict(False, reason)
        if reason := self._check_budget("review"):
            return Verdict(False, f"The spending limit is reached ({reason}), so Kayden "
                                  "could not check this itself.")
        assert self._conversation is not None
        history = await self._memory.history(self._conversation.id, limit=REVIEW_HISTORY)
        info = self._maintenance_info()
        messages = review_messages(user_requests(history), self._bare(call), spec,
                                   self._conversation.folder)
        request = InferenceRequest(info.id, REVIEW_PROMPT, messages, (),
                                   min(REVIEW_OUTPUT_TOKENS,
                                       info.max_output_tokens or REVIEW_OUTPUT_TOKENS),
                                   maintenance=True)
        task = asyncio.ensure_future(self._attempt(request, info, foreground=False))
        if await self._race_stop(task):
            return Verdict(False, "Stopped.")
        try:
            completed = task.result()
        except Exception as e:  # noqa: BLE001 - module calls are wrapped; failing closed
            logger.info("permission review failed: %s", e)
            verdict = Verdict(False, "Kayden could not check this itself.")
        else:
            verdict = parse(
                "".join(b.text for b in completed.message.blocks if isinstance(b, Text)))
        self._log.decision("permission_review", call_id=call.id, allow=verdict.allow)
        return verdict

    # ── Proposals ────────────────────────────────────────────────────────

    def _propose(self, call: ToolCall) -> ToolResult:
        """Put the proposed call before the user as an operation that returns at once."""
        if error := self._make_proposal(call, call.arguments["call"], call.arguments["reason"]):
            return ToolResult(call.id, ToolStatus.ERROR, error)
        return ToolResult(call.id, ToolStatus.BACKGROUNDED,
                          f"Proposed to the user as {call.id}. You are woken with the outcome: "
                          "the call's result if they approve it, or that they dismissed it. "
                          "Carry on, or end your turn.")

    def _wrap_up(self, call: ToolCall) -> ToolResult:
        """Show the card; an option with a call is a card proposal pending with it."""
        actions = [o for o in call.arguments.get("options", ()) if "call" in o]
        if len(actions) > 1:
            return ToolResult(call.id, ToolStatus.ERROR,
                              "Not shown: at most one option may carry a call.")
        self._log.decision("wrap_up", options=len(call.arguments.get("options", ())),
                           action=bool(actions))
        if not actions:
            return ToolResult(call.id, ToolStatus.OK, "Shown to the user; your turn has ended.")
        option = actions[0]
        if error := self._make_proposal(call, option["call"], option["label"], card=True):
            return ToolResult(call.id, ToolStatus.ERROR, f"Not shown: {error}")
        return ToolResult(call.id, ToolStatus.BACKGROUNDED,
                          f"Shown to the user; your turn has ended. If they click "
                          f"{option['label']!r}, its call runs and you are woken with the "
                          "result; if they write instead, it is withdrawn.")

    def _make_proposal(self, call: ToolCall, raw: Mapping[str, Any], reason: str,
                       card: bool = False) -> str | None:
        """Start the operation `call.id` that runs `raw` once the user approves it; an error
        instead if `raw` is not a call to propose."""
        proposed = ToolCall(f"{call.id}-call", raw["name"], dict(raw.get("arguments") or {}))
        if proposed.name not in self._tool_owner:
            what = ("is not a tool you can call" if proposed.name not in self._specs
                    else "needs no approval: call it directly")
            return f"{proposed.name!r} {what}. Propose a call of an action tool."
        if error := self._validate(proposed, self._specs):
            return f"The proposed call: {error}"
        proposal = Proposal(call.id, proposed, reason, card)
        decided: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._proposals[call.id] = (proposal, decided)
        self._proposal_ops.add(call.id)
        spec = self._specs[proposed.name]
        # Waiting for the user is not activity (the duration budget), as a prompt is not.
        self._uncounted.add(call.id)
        op = self._launch(call, replace(BUILTIN_TOOLS["propose"], timeout=math.inf),
                          execute=lambda ctx: self._proposal_outcome(proposal, decided, spec,
                                                                     ctx))
        op.backgrounded = True
        self._log.decision("proposal", call_id=call.id, tool=proposed.name, card=card)
        self._emit(ProposalMade(proposal))
        self._emit(ToolBackgrounded(call.id))
        return None

    async def _proposal_outcome(self, proposal: Proposal, decided: asyncio.Future[bool],
                                spec: ToolSpec, ctx: Context) -> ToolOutput:
        try:
            accepted = await decided
        except asyncio.CancelledError:
            if self._proposals.pop(proposal.id, None) is not None:
                self._log.decision("proposal_withdrawn", call_id=proposal.id)
                self._emit(ProposalResolved(proposal.id, None))
            raise
        if not accepted:
            return ToolOutput("The user dismissed the proposal; it did not run.")
        if reason := self._budget.reserve_tool_call():
            return ToolOutput(f"Approved, but refused: budget {reason}.", is_error=True)
        self._uncounted.discard(proposal.id)
        self._budget.begin_activity()
        self._emit(UsageUpdated(self._budget.snapshot()))
        owner = self._tool_owner.get(proposal.call.name)
        if owner is None:
            return ToolOutput(f"Approved, but {proposal.call.name!r} is no longer offered.",
                              is_error=True)
        timeout = spec.timeout or self._config.default_tool_timeout_s
        try:
            async with asyncio.timeout(None if math.isinf(timeout) else timeout):
                return await owner.execute(self._bare(proposal.call), ctx)
        except TimeoutError:
            return ToolOutput(f"Approved, but it timed out after {timeout:g} s.",
                              is_error=True)

    async def resolve_proposal(self, proposal_id: CallId, accept: bool) -> None:
        entry = self._proposals.pop(proposal_id, None)
        if entry is None:
            raise RuntimeStateError(f"no proposal {proposal_id} is pending")
        proposal, decided = entry
        self._log.decision("permission", call_id=proposal.call.id, by="proposal",
                           outcome=Decision.ALLOW_ONCE if accept else Decision.DENY)
        self._emit(ProposalResolved(proposal_id, accept))
        decided.set_result(accept)
        self._update_state()

    async def _withdraw_proposals(self, cards_only: bool = False) -> None:
        pending = [self._ops[i] for i, (p, _) in self._proposals.items()
                   if i in self._ops and (p.card or not cards_only)]
        for op in pending:
            op.request_cancel()
        supervisors = {op.supervisor for op in pending if op.supervisor is not None}
        if supervisors:
            await asyncio.wait(supervisors)
        if self._flush_tasks:
            await asyncio.wait(self._flush_tasks)

    def _launch(self, call: ToolCall, spec: ToolSpec,
                execute: Callable[[Context], Awaitable[ToolOutput]] | None = None) -> Operation:
        op = Operation(call, spec, default_timeout=self._config.default_tool_timeout_s,
                       grace=self._config.cancel_grace_s, on_progress=self._on_progress,
                       on_late=self._on_late)
        self._ops[call.id] = op
        self._known_ops[call.id] = op
        if call.id not in self._uncounted:
            self._budget.begin_activity()
        self._watch_duration()
        assert self._conversation is not None
        ctx = Context(op, self._conversation.id)
        self._emit(ToolStarted(call))
        op.supervisor = asyncio.create_task(self._supervise(op, ctx, execute))
        self._update_state()
        return op

    async def _supervise(self, op: Operation, ctx: Context,
                         execute: Callable[[Context], Awaitable[ToolOutput]] | None = None
                         ) -> ToolResult:
        try:
            if execute is None:
                owner = self._tool_owner[op.call.name]
                result = await op.run(lambda: owner.execute(self._bare(op.call), ctx))
            else:
                result = await op.run(lambda: execute(ctx))
        except Exception as e:  # noqa: BLE001 - module calls are wrapped
            result = ToolResult(op.call.id, ToolStatus.ERROR, f"Runtime error: {e}")
        op.result = result
        if op.call.id in self._uncounted:
            self._uncounted.discard(op.call.id)
        else:
            self._budget.end_activity()
        del self._ops[op.call.id]
        if op.call.id in self._proposal_ops:
            self._proposal_ops.discard(op.call.id)
            self._proposal_done = True
        if op.abandoned:
            self._log.decision("abandoned", call_id=op.call.id)
            if self._stopping:
                self._stop_abandoned.append(op.call.id)
        if op.backgrounded:
            self._pending_results.append((
                OperationResult(result.call_id, result.status, result.content),
                ToolFinished(result, op.duration),
            ))
            if not self._cycle_active and not self._stopping:
                task = asyncio.create_task(self._flush_outside_cycle())
                self._flush_tasks.add(task)
                task.add_done_callback(self._flush_tasks.discard)
        self._update_state()
        return result

    def _on_progress(self, op: Operation, text: str) -> None:
        self._emit(ToolProgress(op.call.id, text))

    def _on_late(self, op: Operation, result: ToolResult) -> None:
        self._log.decision("late_completion", call_id=op.call.id, status=result.status)
        self._emit(ToolFinished(result, op.duration))

    # ── Built-in tools ───────────────────────────────────────────────────

    async def _run_builtin(self, call: ToolCall, spec: ToolSpec) -> tuple[ToolResult, float]:
        """A built-in or a Memory tool: never gated, not counted, cancelled by Stop."""
        started = time.monotonic()
        self._emit(ToolStarted(call))
        task = asyncio.ensure_future(self._builtin(call) if call.name in _RUNTIME_TOOLS
                                     else self._memory_call(call))
        if await self._race_stop(task) or task.cancelled():
            return ToolResult(call.id, ToolStatus.CANCELLED, "Cancelled."), 0.0
        try:
            status, content = task.result()
        except Exception as e:  # noqa: BLE001 - module calls are wrapped
            status, content = ToolStatus.ERROR, f"{call.name} failed: {e}"
        if call.name in self._memory_tools and not spec.read_only:
            self._emit(MemoriesChanged())
        return ToolResult(call.id, status, content), time.monotonic() - started

    async def _memory_call(self, call: ToolCall) -> tuple[ToolStatus, str]:
        assert self._conversation is not None
        output = await self._memory.execute(self._bare(call),
                                            _MemoryContext(call.id, self._conversation.id))
        return (ToolStatus.ERROR if output.is_error else ToolStatus.OK), output.content

    async def _builtin(self, call: ToolCall) -> tuple[ToolStatus, str]:
        args = call.arguments
        assert self._conversation is not None
        match call.name:
            case "op_status":
                op = self._known_ops.get(args["call_id"])
                if op is None:
                    return ToolStatus.ERROR, f"Unknown operation {args['call_id']}."
                wait_s = min(args.get("wait_s", 0), _OP_STATUS_MAX_WAIT_S)
                if op.result is None and wait_s > 0:
                    assert op.supervisor is not None
                    await asyncio.wait({op.supervisor}, timeout=wait_s)
                return ToolStatus.OK, self._describe(op)
            case "op_cancel":
                op = self._ops.get(args["call_id"])
                if op is None:
                    return ToolStatus.ERROR, f"Operation {args['call_id']} is not running."
                self._log.decision("cancel_operation", call_id=op.call.id, by="model")
                op.request_cancel()
                assert op.supervisor is not None
                await asyncio.wait({op.supervisor})
                return ToolStatus.OK, (f"Cancellation requested; operation {op.call.id} ended "
                                       f"as {op.supervisor.result().status}.")
            case "update_plan":
                now = time.time()
                try:
                    plan = duties.parse_plan(args["items"], now)
                except ValueError as e:
                    return ToolStatus.ERROR, str(e)
                await self._save_duty(self._conversation.duty, plan)
                self._plan_call = call.id
                self._log.decision("plan", items=len(plan), by="model")
                return ToolStatus.OK, duties.saved(plan, self._conversation.duty, now)
            case "ask_for_help":
                if self._conversation.duty is None:
                    return ToolStatus.ERROR, "There is no duty to pause."
                self._help = args["question"]
                self._log.decision("help_requested", question_chars=len(self._help))
                self._emit(HelpNeeded(self._help))
                return ToolStatus.OK, ("Asked. Your check-ins pause until your supervisor "
                                       "answers; end your turn.")
            case "finish_goal":
                duty = self._conversation.duty
                if duty is None or duty.kind != DutyKind.GOAL:
                    return ToolStatus.ERROR, "There is no goal to finish."
                await self._save_duty(None, self._conversation.plan)
                self._log.decision("goal_finished")
                return ToolStatus.OK, "Goal reached; the duty has ended."
            case "skill":
                name = args["name"].strip().removeprefix("/")
                loaded = self._skills.get(name)
                if loaded is None:
                    return ToolStatus.ERROR, (f"There is no skill {name!r}. The skills are: "
                                              f"{', '.join(self._skills) or 'none'}.")
                self._log.decision("skill", name=name)
                return ToolStatus.OK, skill_files.render(loaded, args.get("arguments", ""))
        return ToolStatus.ERROR, f"Unknown built-in {call.name!r}."

    def _describe(self, op: Operation) -> str:
        name = f"Operation {op.call.id} ({op.call.name})"
        if op.result is None:
            progress = f" Last progress: {op.last_progress}." if op.last_progress else ""
            return f"{name} is running; {op.duration:.1f}s elapsed.{progress}"
        return f"{name} finished with status {op.result.status}: {op.result.content}"

    # ── Stop ─────────────────────────────────────────────────────────────

    async def _finish_stop(self) -> None:
        if self._cycle_task is not None and not self._cycle_task.done():
            await asyncio.wait({self._cycle_task})
        supervisors = [op.supervisor for op in self._ops.values() if op.supervisor]
        if supervisors:
            await asyncio.wait(supervisors)
        if self._flush_tasks:
            await asyncio.wait(self._flush_tasks)
        # Messages sent before Stop are kept but start nothing; those sent while
        # stopping start a cycle once stopped.
        early, self._inbox = self._inbox[: self._inbox_before_stop], self._inbox[
            self._inbox_before_stop :]
        try:
            await self._flush_results()
            for q in early:
                await self._append(
                    Message(q.message_id, Role.USER, (Text(q.text),), time.time()))
        except _AppendFailed:
            pass
        if self._stop_abandoned:
            self._emit(StopStalled(list(self._stop_abandoned)))
            self._stop_abandoned.clear()
        self._stopping = False
        self._stop_event.clear()
        self._log.decision("stopped")
        self._set_base(AgentState.STOPPED)
        if self._inbox:
            self._request_cycle("message queued while stopping")
