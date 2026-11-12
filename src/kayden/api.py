"""Kayden v1 module API contracts (Draft 3).

Four module classes: Core, Memory, Tools, Frontend. The Runtime owns
orchestration and is the only component that talks to modules; modules never
call each other. Everything async; cancellation is plain asyncio cancellation.

Message conventions:
- ASSISTANT messages hold Text, Reasoning, ToolCall and ServerToolUse blocks.
- TOOL messages hold only ToolResult blocks. The runtime appends exactly one per turn,
  directly after the assistant message, with one result per call (placeholders and
  refusals included).
- USER messages hold Text and OperationResult blocks (OperationResult carries the
  late outcome of a backgrounded call; Cores render it as text for providers).
- Message ids are unique within a conversation. Core assigns the id of the assistant
  message it returns in Completed; the runtime assigns all others.

Runtime guarantees to modules:
- Durability order: an assistant message is appended before any of its tool calls
  starts; each ToolResult is appended before its ToolFinished event; a user message
  is appended before its MessageCommitted event. If append fails, the runtime emits
  Failure(INTERNAL) and starts nothing further until an append succeeds: the agent is
  FAILED, failed messages wait in memory and are re-appended (idempotently) before any
  later append, and activate() retries. When an assistant message failed, a TOOL
  message with ERROR "Not run" results for its calls waits behind it, so the replayed
  transcript stays well formed. Duplicate call or message ids get no special handling;
  a collision surfaces as an append failure.
- Mutating Memory calls (append, maintain) are serialized per conversation. maintain
  runs before every inference (the first of a cycle included, after the user message
  is appended), after a turn's results are appended and before the next assemble, and
  once after a cycle's last answer is appended, before the agent goes idle; never
  concurrently with inference or appends. Stop cancels it; the runtime re-checks the
  budget after it returns. Inference, maintain and built-in Memory calls get Stop's
  grace period like operations; one that does not stop in time is reported as
  Failure(INTERNAL) and Stop proceeds without it.
- Resume repair: before the first inference of a resumed conversation the runtime
  reads the full transcript via Memory.history and appends ToolResult(INTERRUPTED)
  for every ToolCall without a ToolResult, and OperationResult(INTERRUPTED) for every
  BACKGROUNDED call without an OperationResult: the ToolResults as one TOOL message at
  the tail, then the OperationResults as one USER message.
- Tool calls are validated (known name, arguments match the schema) before they
  start; violations are answered with ToolResult(ERROR) and the tool is not run. A
  known name is one offered in this turn's request: the runtime's built-ins, then Memory's
  tools, then the Core's (which its provider runs), then each Tools module's in configured
  order (several may run at once), each listed once per request. A name already offered
  (a reserved one included) is dropped from the later module's offer and logged; each
  call goes to the module that offered its name. If a list_tools fails,
  Failure(INTERNAL) is emitted and the turn offers the others only.
- Every module call is wrapped: an exception becomes a Failure event (an ERROR
  ToolResult for a tool), never a crash. An unexpected exception from Core.stream is
  treated as a permanent CoreError. A failed maintain is Failure(MODEL) for CoreError
  and Failure(INTERNAL) otherwise; BudgetExhausted is reported by the budget re-check,
  not as a maintain failure.
- One inference cycle runs at a time per runtime. Triggers (user message, wake-up)
  only set flags that the cycle loop consumes.
- Turn boundary: reached once the current turn's foreground ToolResults are appended.
  At a boundary the runtime appends, in order: foreground ToolResults; results of
  background operations that terminated (OperationResult, in one USER message);
  queued user messages (one USER message each, in send order). A turn without tool
  calls ends the cycle, unless queued user messages are delivered at that boundary: they
  authorize the next turn and the cycle continues. The wake set (every operation
  outstanding then) is computed at that boundary: when all of them have terminated (with
  a duty, the first of them), the runtime starts the next cycle by itself, so the agent can
  report on work it started. A duty also wakes it on its schedule (see Duty).
- Budgets: a tool start checks every dimension and counts the call in one atomic step.
  Inference checks every dimension before it starts (nothing is reserved, since its cost
  is unknown) and is cancelled as soon as in-flight usage exhausts one.
"""

from __future__ import annotations

import abc
import os
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

type JSON = dict[str, Any]
type ConversationId = str
type CallId = str  # also the operation id of a backgrounded call
type TokenCounter = Callable[[str | Sequence[Message]], int]  # fast local estimate

PERMISSION_CEILING_ENV = "KAYDEN_PERMISSION_CEILING"
# OS keychain service under which API keys are stored, one entry per key name
# (ModuleInfo.key_name). Cores read their key from the environment first, then from here.
KEYCHAIN_SERVICE = "kayden"


@dataclass(frozen=True, slots=True)
class InstancePaths:
    """Passed to every module factory: factory(options: dict, paths: InstancePaths)."""

    data_dir: Path


# ── Shared types ──────────────────────────────────────────────────────────


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolStatus(StrEnum):
    """Tools produce only OK or ERROR; the rest are assigned by the runtime."""

    OK = "ok"
    ERROR = "error"
    BACKGROUNDED = "backgrounded"  # placeholder: the call is still running
    CANCELLED = "cancelled"  # stopped, termination confirmed (or never started: Stop)
    ABANDONED = "abandoned"  # cancel or timeout not confirmed in time; may still have effects
    TIMED_OUT = "timed_out"  # stalled or over its hard timeout
    INTERRUPTED = "interrupted"  # process died while the call was in flight
    REFUSED = "refused"  # never started: permission denied or budget exhausted


@dataclass(frozen=True, slots=True)
class Text:
    text: str


@dataclass(frozen=True, slots=True)
class Reasoning:
    text: str
    state: JSON | None = None  # opaque provider data (signatures); Core round-trips it
    # Producing model; Cores drop state from other models. None (blocks persisted before
    # this field) counts as another model, so such reasoning is never replayed.
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: CallId
    name: str
    arguments: JSON
    raw_arguments: str | None = None  # set when the model's arguments did not parse


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: CallId
    status: ToolStatus
    content: str


@dataclass(frozen=True, slots=True)
class OperationResult:
    """Final outcome of a call whose ToolResult was a BACKGROUNDED placeholder."""

    call_id: CallId
    status: ToolStatus
    content: str


@dataclass(frozen=True, slots=True)
class ServerToolUse:
    """A tool the provider ran itself during the turn (Core.list_tools), call and outcome in
    one block of the assistant message; no ToolResult follows. `summary` says in a line
    what it did ("Searched the web for …") and `content` what it found, readable: both
    are shown, searched and given as text to other providers. `state` is opaque provider
    data its Core replays to that provider."""

    id: str
    name: str
    input: JSON
    summary: str
    content: str = ""
    is_error: bool = False
    state: JSON | None = None


@dataclass(frozen=True, slots=True)
class OpaqueBlock:
    """A persisted block whose type this version doesn't know (for example a removed
    plugin's). Memory returns it for unresolvable tags and re-encodes it unchanged;
    Cores skip it."""

    type: str
    data: JSON


type Block = (Text | Reasoning | ToolCall | ToolResult | OperationResult | ServerToolUse
              | OpaqueBlock)

# Stable on-disk tags. Memory persists blocks as {"type": tag, "data": {...}} with these
# tags, never Python import paths, so renaming a class doesn't break an archive.
BLOCK_TYPES: Mapping[str, type] = {
    "text": Text,
    "reasoning": Reasoning,
    "tool_call": ToolCall,
    "tool_result": ToolResult,
    "operation_result": OperationResult,
    "server_tool_use": ServerToolUse,
}


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    role: Role
    blocks: tuple[Block, ...]
    created_at: float  # unix seconds


@dataclass(frozen=True, slots=True)
class Usage:
    """input_tokens includes cached_tokens."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float | None = None  # None when the provider doesn't report it
    # What the provider charges beyond the token prices (its own tools' uses, the premium
    # for writing its prompt cache), added to the cost whether or not cost_usd is reported.
    extra_cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: JSON  # JSON Schema object
    read_only: bool = False  # consecutive read-only calls run concurrently; others are barriers
    stall_timeout: float | None = None  # seconds without progress() before the run is
    #                                     treated as hung; None = no stall detection
    timeout: float | None = None  # hard wall-clock limit; None = runtime default
    #                               (suggested 1 h); math.inf = no limit


# ── Core ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    name: str
    context_window: int | None  # None = unknown; the runtime then requires configuration
    max_output_tokens: int | None = None
    # Needed to enforce max_cost_usd when the provider doesn't report cost.
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    cached_input_usd_per_mtok: float | None = None


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    model: str
    system: str
    messages: Sequence[Message]
    tools: Sequence[ToolSpec]
    max_output_tokens: int | None = None
    # Set on the runtime's own one-shot requests (Memory maintenance through Completion,
    # permission reviews), never on a turn.
    maintenance: bool = False


class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"  # a truncated tool call is answered with ERROR
    # Model declined; treated like END_TURN, shown distinctly. The runtime runs a turn's
    # tool calls whatever its stop reason, so a refusal that carries calls still gets
    # their results.
    REFUSAL = "refusal"


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """Optional: Cores that can't stream tool arguments simply never emit it."""

    call_id: CallId
    name: str | None  # known from the first delta on
    arguments_chunk: str  # raw fragment of the JSON arguments; not parseable alone


@dataclass(frozen=True, slots=True)
class UsageReport:
    """Cumulative usage for this request, emitted whenever the provider reports it."""

    usage: Usage


@dataclass(frozen=True, slots=True)
class Completed:
    """Always the last event. `message` is the full assistant turn, tool calls included."""

    message: Message
    usage: Usage
    stop: StopReason


type CoreEvent = TextDelta | ReasoningDelta | ToolCallDelta | UsageReport | Completed


class CoreError(Exception):
    """Failure from Core. Permanent (never retried) unless it is a TransientCoreError."""


class TransientCoreError(CoreError):
    """Rate limit, timeout, 5xx. The runtime retries with backoff; Core must not."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ContextOverflowError(CoreError):
    """The request exceeded the model's context window. The runtime re-assembles with
    90% of the assembled-context budget (not of the window) and retries once before
    failing; that retry emits ModelRetry (delay 0) and does not count as a transient
    retry. Providers have no dedicated error for this, so Cores recognise it from known
    codes and message fragments on 400/413 responses; an unrecognised wording stays a
    permanent CoreError."""


class Core(abc.ABC):
    """Model component. Tool calling is mandatory."""

    @abc.abstractmethod
    async def list_models(self) -> Sequence[ModelInfo]: ...

    @abc.abstractmethod
    def stream(self, request: InferenceRequest) -> AsyncIterator[CoreEvent]:
        """Implement as an async generator (it must support aclose()). Cancel by
        cancelling the consuming task.

        Emit UsageReport as soon as the provider reports usage. Reports may lag far
        behind the stream (many providers report output only at the end), so if the
        request is cancelled or fails before Completed, the runtime charges, per
        dimension, the larger of the last report and its own estimate (input plus
        streamed output). A failure before the first stream event is not charged; a
        cancellation (Stop, duration budget) always is, since the provider may have
        started processing. The runtime iterates with contextlib.aclosing. An empty
        ToolCallDelta chunk is allowed, to announce a call before its arguments."""

    async def list_tools(self) -> Sequence[ToolSpec]:
        """Optional: tools the provider runs during a request itself (its web search, for
        example). The runtime offers them after its own and Memory's and before the Tools
        modules', sends them in the request (the Core turns each into the provider's own
        tool by name), and never runs, gates or counts them: they touch nothing on this
        computer. Each use comes back in Completed as a ServerToolUse block; what the
        provider charges for it, in Usage.extra_cost_usd."""
        return ()

    async def count_tokens(self, request: InferenceRequest) -> int | None:
        """Optional exact count. The runtime only uses it to calibrate its own sync
        estimator; return None if the provider has no counting endpoint."""
        return None

    async def aclose(self) -> None:
        pass


# ── Memory ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ConversationInfo:
    """Conversation metadata. Not part of the append-only transcript archive."""

    id: ConversationId
    title: str | None  # None = not set yet; Memory writes one during maintain
    created_at: float
    updated_at: float
    archived: bool = False  # hidden from list_conversations() and recall(scope="all")
    seed_from: ConversationId | None = None  # "Continue from" source; None once deleted
    preview: str | None = None  # start of the first user message, for display
    folder: str | None = None  # work folder, an absolute path; None = the Tools' default
    duty: Duty | None = None  # standing work the conversation does by itself
    plan: Sequence[PlanItem] = ()  # kept when the duty ends


class DutyKind(StrEnum):
    DUTY = "duty"  # runs until a person turns it off
    GOAL = "goal"  # also ends when the agent declares it reached (finish_goal)


@dataclass(frozen=True, slots=True)
class Duty:
    """Standing work: a conversation with a duty continues by itself. Only a person sets
    one (RuntimeAPI.set_duty). While the agent is idle it wakes on the first operation to
    finish (not the whole batch), on a message, and at the earliest due open plan item,
    held between min_interval_s and max_interval_s after the cycle ends; with nothing due,
    at max_interval_s."""

    instruction: str
    kind: DutyKind = DutyKind.DUTY
    min_interval_s: float = 60.0
    max_interval_s: float = 3600.0


class PlanStatus(StrEnum):
    TODO = "todo"
    DOING = "doing"
    DONE = "done"
    DROPPED = "dropped"


@dataclass(frozen=True, slots=True)
class PlanItem:
    """One step of a conversation's plan, which the agent keeps with update_plan and the
    user edits (RuntimeAPI.set_plan). Every request has the whole plan in view: the agent's
    own call, or the system prompt when the messages do not show it."""

    text: str
    status: PlanStatus = PlanStatus.TODO
    due: float | None = None  # Unix time; an open item due wakes a duty
    note: str = ""


@dataclass(frozen=True, slots=True)
class ContextRequest:
    conversation_id: ConversationId
    # Budget for the assembled context: window minus base system prompt, tool schemas and
    # the output reserve (the model's max_output_tokens or the runtime default, capped at
    # a quarter of the window; also sent as InferenceRequest.max_output_tokens).
    max_tokens: int
    count_tokens: TokenCounter


@dataclass(frozen=True, slots=True)
class AssembledContext:
    system_addendum: str  # rolling summary + long-term memory, rendered for the prompt
    messages: Sequence[Message]  # recent verbatim tail


@dataclass(frozen=True, slots=True)
class ArchiveHit:
    conversation_id: ConversationId
    message: Message
    score: float | None = None


PROFILE = "About you"  # the built-in Memory's note every request sees in full


@dataclass(frozen=True, slots=True)
class Skill:
    """Instructions for one kind of task, which the model loads when the task comes up
    (SKILL_TOOL) or the user asks for by writing /name as a message. A folder with a
    SKILL.md in Claude Code's format: Kayden's own, the user's in <data dir>/skills, a
    project's in <work folder>/.claude/skills; a later one replaces an earlier one of the
    same name."""

    name: str  # lowercase letters, digits and hyphens
    description: str  # one line: what it does and when to use it


@dataclass(frozen=True, slots=True)
class MemoryNote:
    """One entry of long-term memory, as the Memory module keeps it: the built-in Memory's
    notes (a profile, titled PROFILE, and one note per topic), or another module's (a file
    of a project's memory folder). Frontends list them by title and description and edit
    all three; what each request sees of them is the module's."""

    id: str
    title: str  # unique, case ignored
    description: str  # one line: what the note is about, for the index
    text: str
    # The conversation that last wrote it; None = written by the user, or its conversation
    # was deleted.
    source_conversation_id: ConversationId | None
    updated_at: float
    fixed: bool = False  # cannot be renamed or deleted, only emptied (the built-in profile)


@dataclass(frozen=True, slots=True)
class MemoryView:
    """Read-only, display-oriented snapshot of what Memory currently holds."""

    summary: str
    long_term: Sequence[str]  # the profile, then the index: what every request sees


class BudgetExhausted(Exception):
    """Raised by Completion once a hard budget is spent; Memory should abort cleanly."""


class Completion(Protocol):
    """Metered one-shot inference handed to Memory by the runtime.

    Counts against the instance budget. Raises BudgetExhausted when a hard budget is
    spent, or CoreError if the model fails after the runtime's own retries.
    light=True asks for the runtime's cheaper maintenance model (falls back to the
    main model if none is configured); Memory never names a model itself. max_tokens is
    capped at that model's output limit. Requests carry no tools and maintenance=True.
    Transient errors are retried with the runtime's backoff but emit no ModelRetry
    (nothing streams to frontends). The first call of a maintain emits MaintenanceStarted;
    MaintenanceFinished follows when maintain returns. compacting=True marks the call that
    summarizes older messages out of the active context, so frontends can show it:
    CompactionStarted when it starts, CompactionFinished when maintain returns. The result
    may be empty (a model that spends its whole output limit on reasoning); Memory must then
    change nothing.
    """

    def max_input_tokens(self, *, light: bool = False) -> int:
        """Context window of the model that serves the given kind of call, so Memory
        can size what it compacts."""

    async def __call__(
        self,
        system: str,
        messages: Sequence[Message],
        *,
        max_tokens: int,
        light: bool = False,
        compacting: bool = False,
    ) -> str: ...


class Memory(abc.ABC):
    """Owns transcripts, context assembly, compaction, archive, long-term memory.

    Persist blocks generically using BLOCK_TYPES tags, never assuming the current Block variants
    are the only ones, so new block types round-trip without a storage migration.

    Every method that takes a conversation id raises KeyError for an unknown one, and
    every method that takes a memory entry id raises KeyError for an unknown entry.
    Long-term memory is one list per store, shared by every conversation.
    The archive is append-only except for delete_conversation, which the user asks for.
    """

    @abc.abstractmethod
    async def create_conversation(
        self,
        *,
        title: str | None = None,
        seed_from: ConversationId | None = None,
        folder: str | None = None,
        conversation_id: ConversationId | None = None,
    ) -> ConversationInfo:
        """seed_from (KeyError if unknown) starts a conversation that continues another:
        on its first maintain, Memory writes its initial rolling summary from the
        source's current summary and verbatim tail. The source is not changed. folder is
        stored as given (ConversationInfo.folder). conversation_id: the id to use (the
        runtime names a new chat before storing it); ValueError if it exists. None: Memory
        picks one."""

    @abc.abstractmethod
    async def list_conversations(
        self, *, include_archived: bool = False
    ) -> Sequence[ConversationInfo]:
        """Newest activity first."""

    @abc.abstractmethod
    async def update_conversation(
        self,
        conversation_id: ConversationId,
        *,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationInfo:
        """Change metadata; None leaves a field as it is. A title is stripped of surrounding
        whitespace; an empty one clears it, so an automatic title follows. Returns the
        updated info."""

    @abc.abstractmethod
    async def append(
        self, conversation_id: ConversationId, messages: Sequence[Message]
    ) -> None:
        """Durable and crash-safe on return. Also feeds the permanent archive.

        Idempotent by message id: re-appending an identical message is a no-op (a
        cancelled append may have landed); the same id with different content raises
        ValueError and nothing from the batch is appended."""

    @abc.abstractmethod
    async def history(
        self,
        conversation_id: ConversationId,
        *,
        before: str | None = None,
        limit: int | None = None,
        after: str | None = None,
    ) -> Sequence[Message]:
        """Full transcript from the archive, in append order, regardless of compaction.

        With `before` (a message id), only messages preceding it; with `after`, only
        messages following it (KeyError for an unknown id). With `limit`, only the last
        `limit` of those, or with `after` the first: reading on from a message."""

    @abc.abstractmethod
    async def assemble(self, request: ContextRequest) -> AssembledContext:
        """Read-only, no inference. Must fit request.max_tokens (addendum included)
        even if maintenance was deferred, by trimming the verbatim tail, never the
        archive. The returned messages are a suffix of the transcript that never splits a
        ToolCall from its ToolResult, always includes the newest message, and starts at a
        USER message when one qualifies (else at the latest message that splits no call).
        A single oversized message is truncated in this view only (the stored copy is
        untouched), with a marker naming the message id and recall: Reasoning blocks are
        dropped, Text and result bodies are cut, ToolCall blocks stay whole. If even the
        newest exchange, truncated, exceeds the budget, it is returned anyway: including it
        wins over fitting, and the Core's ContextOverflowError handles the rest."""

    @abc.abstractmethod
    async def maintain(
        self,
        conversation_id: ConversationId,
        complete: Completion,
        context: ContextRequest,
    ) -> bool:
        """Compaction / consolidation / forgetting. Memory decides whether to act and
        returns whether it did. `context` gives the current window so thresholds can be
        fractions of it (the model may change mid-conversation). The call after a cycle's
        last answer is where Memory saves what the exchange taught it.

        Called by the runtime only inside an authorized inference cycle, so it can
        never restart an agent by itself. Archive writes and compaction-state updates must
        be atomic: the only copy of a message is never removed. BudgetExhausted must
        propagate with no state change.

        Also writes the initial summary of a seeded conversation (see
        create_conversation) and, once the first user message is in, a title for a
        conversation that has none (light model, at most 20 output tokens). A failed or
        empty title call leaves the title empty, to be retried by a later maintain; a
        title that is set is never overwritten.
        """

    @abc.abstractmethod
    async def search(
        self,
        query: str,
        *,
        conversation_ids: Collection[ConversationId] | None = None,
        limit: int = 5,
    ) -> Sequence[ArchiveHit]:
        """Read-only archive search; backs recall() and RuntimeAPI.search_archive().

        `query` is plain text with no operator syntax. conversation_ids limits the search
        to those conversations (None = every conversation). Higher score is better and is
        comparable only within one result set."""

    async def inspect(self, conversation_id: ConversationId) -> MemoryView | None:
        """Optional. Return None to expose nothing to frontends."""
        return None

    # Optional: tools the model can call to work with memory (kayden.memory.tools has
    # recall, remember and forget, built on this contract). They run without permission
    # prompts and outside the tool-call budget: they read and change only Kayden's own
    # memory, which the user manages in the Memory window. ctx.conversation_id is the open
    # conversation. After a tool that is not read_only, the runtime emits MemoriesChanged.

    async def list_tools(self) -> Sequence[ToolSpec]:
        """Called once per request, like Tools.list_tools."""
        return ()

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutput:
        raise NotImplementedError

    # Optional: long-term memory notes the user can see and change. The base class raises
    # NotImplementedError; the runtime then reports that memory cannot be edited.

    # `folder` is the project the notes belong to (a conversation's work folder, None for
    # none), for a module that keeps memory per project; a module with one memory for all
    # conversations ignores it.

    async def notes(self, *, folder: str | None = None) -> Sequence[MemoryNote]:
        """In the order the module shows them: fixed notes first."""
        raise NotImplementedError

    async def save_note(self, title: str, description: str, text: str, *,
                        note_id: str | None = None,
                        conversation_id: ConversationId | None = None,
                        folder: str | None = None) -> MemoryNote:
        """Write a note: the one `note_id` names (KeyError if unknown; its title may
        change), else the one with this title, else a new one. ValueError for an empty or
        invalid title, a title another note has, or renaming a fixed note.
        `conversation_id` is where the agent wrote it; None, the user."""
        raise NotImplementedError

    async def delete_note(self, note_id: str, *, folder: str | None = None) -> None:
        """Erase a note (with its revisions, where kept). KeyError if unknown; ValueError for
        a fixed note, which can only be emptied."""
        raise NotImplementedError

    async def save_duty(self, conversation_id: ConversationId, duty: Duty | None,
                        plan: Sequence[PlanItem]) -> ConversationInfo:
        """Optional. Replace the conversation's duty and plan (ConversationInfo.duty, plan),
        stored as metadata. KeyError if unknown. create_conversation never copies them from
        seed_from."""
        raise NotImplementedError

    async def delete_conversation(self, conversation_id: ConversationId) -> None:
        """Optional. Erase a conversation: messages, summaries, search index entries,
        and metadata. Notes it wrote stay, with no source conversation: they are managed in
        long-term memory (delete_note).
        Conversations seeded from it keep their own summaries; their seed_from becomes
        None. The runtime never calls it for the open conversation."""
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


# ── Tools ─────────────────────────────────────────────────────────────────

_CALL_ID = {"type": "string", "description": "Id of the tool call."}

# Runtime built-ins, about the runtime's own operations. Never gated by permissions; they
# don't count as tool calls for budgets.
# op_status and op_cancel answer ERROR for an unknown or finished id; operations from
# before a restart are unknown, their outcome is the INTERRUPTED OperationResult from
# resume repair. op_cancel waits for termination (bounded by the grace period) and
# reports the final status; the outcome still arrives as an OperationResult.
_REPLIES = {"type": "array", "minItems": 1, "maxItems": 3,
            "items": {"type": "string", "minLength": 1, "maxLength": 80}}

BUILTIN_TOOLS: Mapping[str, ToolSpec] = {
    "op_status": ToolSpec(
        "op_status",
        "Check a backgrounded call. Optionally wait up to wait_s seconds for it to finish (values above 60 are treated as 60).",
        {
            "type": "object",
            "properties": {
                "call_id": _CALL_ID,
                "wait_s": {"type": "number", "minimum": 0, "default": 0},
            },
            "required": ["call_id"],
        },
        read_only=True,
    ),
    "op_cancel": ToolSpec(
        "op_cancel",
        "Request cancellation of a backgrounded call, or withdraw a proposal.",
        {"type": "object", "properties": {"call_id": _CALL_ID}, "required": ["call_id"]},
    ),
    # A proposal is an operation with the propose call's id; see Proposal.
    "propose": ToolSpec(
        "propose",
        "Propose a tool call that is your own idea, not something the user asked for: the "
        "user sees it with your reason and approves or dismisses it. It returns at once; "
        "carry on or end your turn. If approved, exactly this call runs and you are woken "
        "with its result; if dismissed, you are woken with that. Call tools the user asked "
        "for directly instead.",
        {
            "type": "object",
            "properties": {
                "call": {
                    "type": "object",
                    "properties": {"name": {"type": "string"},
                                   "arguments": {"type": "object", "default": {}}},
                    "required": ["name"],
                    "description": "The tool call to run if the user approves.",
                },
                "reason": {"type": "string", "minLength": 1,
                           "description": "One line: why it is worth doing."},
            },
            "required": ["call", "reason"],
        },
    ),
    # Ends the turn when it is the only call in its message (no request follows); frontends
    # show it as a card from the transcript (see SUGGESTION_TOOLS). An option with a call is
    # a Proposal with card=True and the wrap_up call's id.
    "wrap_up": ToolSpec(
        "wrap_up",
        "Only when your turn leaves the user a decision (go ahead or not, which option): end "
        "it with a card. question: one short sentence. options: 1 to 3 buttons that answer "
        "it, your recommendation first, each label written as the user would say it (\"Yes, "
        "push\", \"Fix the first one\", \"Not now\"); a click sends it as their message. At "
        "most one option may carry a call: clicking it runs exactly that call, and you are "
        "woken with its result. recap, optional: one or two sentences on what is done and "
        "not done, when the decision needs it. Call wrap_up alone, last, after your other "
        "work: your turn ends with it.",
        {
            "type": "object",
            "properties": {
                "recap": {"type": "string", "minLength": 1, "maxLength": 400},
                "question": {"type": "string", "minLength": 1, "maxLength": 100},
                "options": {
                    "type": "array", "minItems": 1, "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "minLength": 1, "maxLength": 40},
                            "call": {
                                "type": "object",
                                "properties": {"name": {"type": "string"},
                                               "arguments": {"type": "object", "default": {}}},
                                "required": ["name"],
                                "description": "A tool call to run when this option is clicked.",
                            },
                        },
                        "required": ["label"],
                    },
                },
            },
            "required": ["question", "options"],
        },
    ),
}

_PLAN_ITEM = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": [s.value for s in PlanStatus], "default": "todo"},
        "due": {"type": "string", "description":
                "When it falls due, ISO 8601 (e.g. 2026-11-12T09:00), local time unless an "
                "offset is given."},
        "due_in_minutes": {"type": "number", "minimum": 0,
                           "description": "Instead of due: minutes from now."},
        "note": {"type": "string"},
    },
    "required": ["text"],
}

# Runtime built-ins offered with an open conversation (update_plan), while it has a duty
# (ask_for_help) or a goal (finish_goal). Like BUILTIN_TOOLS, never gated and not counted.
DUTY_TOOLS: Mapping[str, ToolSpec] = {
    "update_plan": ToolSpec(
        "update_plan",
        "Replace your plan with this whole list; the user sees it as a checklist. For a task "
        "with several steps, write the plan before you start, then mark each step doing and "
        "done as you go, in the same call as other work where you can. Skip it for a quick "
        "answer or a single step. When a new task starts, replace the old plan. Never end "
        "your turn with the plan out of date: when the work is finished, every step is "
        "done or dropped. Your last update stays in view; with a "
        "duty, an open item that falls due wakes you.",
        {"type": "object",
         "properties": {"items": {"type": "array", "items": _PLAN_ITEM}},
         "required": ["items"]},
    ),
    "ask_for_help": ToolSpec(
        "ask_for_help",
        "When you are blocked and only your supervisor can unblock you (a decision, an "
        "approval, access you lack): ask one clear question. Your duty's check-ins pause "
        "until they answer; finished background work still wakes you. Optionally offer 1 to "
        "3 replies they can click, written as they would say them.",
        {"type": "object",
         "properties": {"question": {"type": "string", "minLength": 1}, "replies": _REPLIES},
         "required": ["question"]},
    ),
    "finish_goal": ToolSpec(
        "finish_goal",
        "Declare your goal reached, with a short report for the user. Your duty then ends.",
        {"type": "object", "properties": {"report": {"type": "string", "minLength": 1}},
         "required": ["report"]},
    ),
}

# The runtime built-in offered while there are skills (Skill): it answers with a skill's
# instructions and the folder its files are in. Every request's system prompt lists the
# skills by name and description. Never gated and not counted.
SKILL_TOOL = ToolSpec(
    "skill",
    "Load a skill's instructions by its name, then follow them. Use it when a task fits a "
    "listed skill, or when the user's message starts with /name.",
    {"type": "object",
     "properties": {
         "name": {"type": "string", "minLength": 1},
         "arguments": {"type": "string",
                       "description": "What the user wrote after /name, or what the "
                                      "skill should work on."}},
     "required": ["name"]},
    read_only=True,
)

# Calls frontends show as a card under the answer (wrap_up: recap, question, options;
# ask_for_help: question, replies), while no user message follows it. A click on an option
# sends its label as the user's message, or approves its call's Proposal.
SUGGESTION_TOOLS = frozenset({"wrap_up", "ask_for_help"})

# No module may declare these names.
RESERVED_TOOL_NAMES = frozenset(BUILTIN_TOOLS) | frozenset(DUTY_TOOLS) | {SKILL_TOOL.name}

# Text Kayden itself writes into a conversation as a USER message, for the model (an
# activation, a duty's wake-up, a sub-session's report) starts with this. Frontends show
# such a message as a notice, not as something the user said.
NOTICE_PREFIX = "[Runtime notice] "

# Every tool the runtime offers takes an optional argument under this name: a few plain
# words the model writes about what the call does, which frontends show in place of the
# tool and its input. The runtime adds it to each offered schema and removes it from the
# call before the Tools module sees it (confined, execute); events, snapshots and the
# transcript keep it. The name is one no tool needs; a tool whose own schema declares it
# anyway gets it like any argument, and frontends show it as the step's sentence.
DESCRIPTION_ARG = "step_summary"


class ToolContext(Protocol):
    call_id: CallId
    conversation_id: ConversationId  # the open conversation

    def progress(self, text: str) -> None:
        """Report progress. Also counts as liveness for stall detection. Safe to call
        from any thread. An empty string is a silent heartbeat: liveness only, nothing
        displayed."""


@dataclass(frozen=True, slots=True)
class ToolOutput:
    content: str
    is_error: bool = False


class Tools(abc.ABC):
    """Tool provider. One coroutine per call; the runtime owns everything else.

    The runtime runs execute() as a task, backgrounds it (the model gets the call id
    as the operation id) if it outlives the initial wait, and polls or cancels via task
    state. Consecutive read-only calls in a turn run concurrently; a non-read-only call
    is a barrier: it starts once every earlier call has finished or been backgrounded,
    and later calls wait for it likewise. Tools only need to handle
    asyncio.CancelledError by cleaning up promptly.

    Never block the event loop: blocking work goes through asyncio.to_thread or a
    subprocess. A blocked loop freezes Stop for the whole instance. A thread cannot be
    cancelled, so thread work must be short or cooperatively cancellable; prefer a
    subprocess for long blocking work.

    After cancellation execute() must return within the runtime's cancel grace period
    (default 5 s) or the call is abandoned. Keep results reasonably small; the runtime
    does not truncate them. The runtime puts the last progress text into the content of
    CANCELLED, TIMED_OUT and ABANDONED results. A call that returns normally after
    cancellation was requested keeps its real OK or ERROR result: its effect happened.

    Tools that launch subprocesses must pass PERMISSION_CEILING_ENV through unmodified, or
    launched Kayden instances escape the ceiling. They may leave out other variables, such
    as secrets from commands that run unasked.
    """

    @abc.abstractmethod
    async def list_tools(self) -> Sequence[ToolSpec]:
        """Called each cycle, so the set may change."""

    @abc.abstractmethod
    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutput: ...

    async def use_folder(self, folder: Path | None) -> None:
        """Optional. Make `folder` the work folder of the calls that follow, creating it
        when missing; None = the module's own default. The runtime calls it whenever it
        opens a conversation, with no call running. Raises OSError when the folder cannot
        be used. NotImplementedError (the default): the module has one fixed folder."""
        raise NotImplementedError

    async def confined(self, call: ToolCall) -> bool:
        """True when this call can read and change nothing on this computer outside the
        module's work folder (a web request reads nothing on it). Optional: False (the
        default) makes the runtime ask under ALLOW_WORKSPACE and review the call under AUTO.
        Called before the permission decision; an exception counts as False.

        If the call then runs, execute() gets the same call (same id), so a module can
        hold execution to what it judged here. The call may also never run (denied,
        stopped, over budget)."""
        return False

    async def aclose(self) -> None:
        pass


# ── Frontend ──────────────────────────────────────────────────────────────


class AgentState(StrEnum):
    NOT_STARTED = "not_started"  # before start()
    IDLE = "idle"  # background ops may still run; see StateChanged.pending_ops
    THINKING = "thinking"
    RUNNING_TOOLS = "running_tools"
    AWAITING_PERMISSION = "awaiting_permission"  # any prompt open; other ops keep running
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class Decision(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_CONVERSATION = "allow_conversation"  # in memory only; keyed by tool name
    DENY = "deny"


class PermissionPolicy(StrEnum):
    """Declared strictest first, so declaration order is strictness order.

    DENY: every module tool is refused (ToolStatus.REFUSED), no prompts.
    ASK: every module tool prompts unless covered by a conversation grant.
    ALLOW_READ_ONLY: read_only tools run; the rest prompt.
    ALLOW_WORKSPACE: calls the Tools module reports as confined to its work folder
    (Tools.confined) run; the rest prompt, read_only ones included.
    AUTO: confined calls run; every other call is reviewed by one request to the
    maintenance model, which sees the text of the user's messages and the call (never tool
    results). A clear allow runs it; anything else, a failed review or an exhausted budget
    included, prompts with the reviewer's reason (PermissionRequest.reason). Reviews are
    charged to the budget and cancelled by Stop.
    Conversation grants apply under ASK and looser. Built-in tools are never gated. With a
    non-interactive frontend, the calls that would have prompted are refused instead;
    automatic allows still apply.
    """

    DENY = "deny"
    ASK = "ask"
    ALLOW_READ_ONLY = "allow_read_only"
    ALLOW_WORKSPACE = "allow_workspace"
    AUTO = "auto"

    def clamp(self, ceiling: PermissionPolicy) -> PermissionPolicy:
        """The stricter of this policy and the ceiling (spec 10.4)."""
        order = list(PermissionPolicy)
        return order[min(order.index(self), order.index(ceiling))]

    @classmethod
    def inherited_ceiling(cls) -> PermissionPolicy | None:
        """The ceiling set by a parent Kayden. The runtime exports its own effective
        policy as the ceiling when it is constructed, before any tool can run."""
        value = os.environ.get(PERMISSION_CEILING_ENV)
        if not value:
            return None
        try:
            return cls(value)
        except ValueError:
            return cls.DENY  # fail closed on a garbled ceiling


class FailureKind(StrEnum):
    MODEL = "model"  # retries exhausted, or a permanent Core error
    BUDGET = "budget"
    INTERNAL = "internal"


class RuntimeStateError(Exception):
    """Command not valid in the current state (see RuntimeAPI)."""


class InstanceLockedError(RuntimeError):
    """Another process uses this data directory. The message is written for the user."""


@dataclass(frozen=True, slots=True)
class Budget:
    """None = unlimited on that dimension.

    max_tokens counts input (cached included) plus output. max_cost_usd needs prices:
    start() and select_model raise ValueError for a model without input and output
    prices whenever it is set. max_duration_s is wall-clock time while at least one
    inference or tool operation runs (background included); permission waits and
    backoff don't count, and exhausting it also cancels running operations as Stop does.
    max_tool_calls excludes built-ins. Exhausting any dimension stops all chargeable
    execution, inference included. A budget belongs to the data dir; usage persists there
    in usage.json.

    A dimension is exhausted when used >= limit. Mid-stream, after every event, in-flight
    usage counts as the larger of the last UsageReport and the runtime's estimate. A
    request whose Completed pushes usage over the limit is still committed; the next
    check refuses. When a budget cuts inference off mid-stream, the streamed text is
    committed (text only, as with Stop) and the agent fails with Failure(BUDGET). An
    abandoned call stops counting toward max_duration_s when it is abandoned.
    """

    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_duration_s: float | None = None
    max_tool_calls: int | None = None


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Read-only to frontends; budget and permissions come from the instance
    configuration. The model is separate: see RuntimeSnapshot.model."""

    budget: Budget
    permissions: PermissionPolicy  # effective: after clamping to any inherited ceiling


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    usage: Usage
    tool_calls: int
    elapsed_s: float
    budget_remaining: dict[str, float] = field(default_factory=dict)  # keyed by Budget field


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    id: str
    call: ToolCall
    read_only: bool
    confined: bool = False  # the call stays inside the Tools' work folder (Tools.confined)
    reason: str | None = None  # AUTO: why the review asks the user


@dataclass(frozen=True, slots=True)
class OperationInfo:
    call: ToolCall  # call.id is the operation id
    started_at: float
    last_progress: str | None = None
    backgrounded: bool = False  # past the initial wait; the model got a placeholder


@dataclass(frozen=True, slots=True)
class StartOptions:
    """conversation_id None = a new conversation, else resume that one (KeyError if
    unknown). seed_from and folder apply to a new conversation only (ValueError with
    conversation_id); seed_from must name an existing conversation. folder is the new
    conversation's work folder (ValueError if it is not a folder, or no Tools module takes
    one; each module that takes folders gets it); without it, seed_from's folder, else Settings.work_folder."""

    conversation_id: ConversationId | None = None
    model: str | None = None
    seed_from: ConversationId | None = None
    folder: str | None = None


MODULE_KINDS = ("core", "memory", "tools", "frontend")


@dataclass(frozen=True, slots=True)
class ModuleInfo:
    """An installed module: an entry point in the group kayden.cores, kayden.memories,
    kayden.tools or kayden.frontends whose package imports cleanly. The entry point's name
    is the id, its target the factory. A factory may carry display_name; a Core factory
    key_name, a Frontend factory kind."""

    id: str  # e.g. "anthropic"
    name: str  # for display, e.g. "Claude"; the id without display_name
    key_name: str | None = None  # Cores: the key's environment variable and keychain entry
    has_key: bool = False  # a key is available from the environment or the keychain
    kind: str | None = None  # Frontends: "terminal" or "window" (FrontendInfo.kind)


@dataclass(frozen=True, slots=True)
class ModuleChoice:
    """One kind of module. running and chosen are None for a module given in config.toml
    by import path, which has no id."""

    running: str | None  # the module running now
    chosen: str | None  # the module used from the next start
    installed: Sequence[ModuleInfo]


@dataclass(frozen=True, slots=True)
class McpServer:
    """An MCP server whose tools Kayden offers: exactly one of `command` (a command line
    Kayden starts) and `url`. `name` (letters, digits, - and _) names its tools
    (mcp__<name>__<tool>). `env` maps the variables the server gets to their values,
    which the OS keychain keeps, never a file: in Settings every value is "" (never
    returned); in SettingsUpdate a value replaces the saved one and "" keeps it."""

    name: str
    command: str | None = None
    url: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Settings:
    """The user settings, in the terms a settings screen shows. Models come from
    list_models(), spending so far from snapshot().usage.

    Runtime settings (model, maintenance model, budget, permissions, system prompt, work
    folder) apply live. Module settings (modules, the API key) are what module factories
    receive: they are saved at once and apply at the next start (restart())."""

    modules: Mapping[str, ModuleChoice]  # keyed by MODULE_KINDS
    model: str | None  # None before start() = the first model the Core lists
    maintenance_model: str | None  # None = the main model
    budget: Budget
    permissions: PermissionPolicy  # effective: clamped to any inherited ceiling
    system_prompt: str
    # The folder new conversations get (StartOptions.folder); None = the Tools have a fixed
    # one. Conversations keep theirs (ConversationInfo.folder).
    work_folder: str | None
    restart_needed: bool  # saved module settings differ from the running instance
    # The frontend restart() brings up: the chosen one, unless it cannot be shown from the
    # running one (a terminal frontend from a window frontend, a window frontend without a
    # display); then the running one, and the chosen one is used at the next launch of its
    # kind. None for a frontend from config.toml.
    restart_frontend: str | None
    # Set when the modules could not be built (no key yet, a module that is not installed,
    # bad options). Only settings and restart() work then; the frontend shows the settings
    # with this message so the user can fix it.
    startup_error: str | None = None
    # With startup_error: another process holds the data directory, so closing it, not
    # changing settings, is the fix.
    already_open: bool = False
    # Module settings: saved at once, started at the next start (one more Tools module).
    mcp_servers: Sequence[McpServer] = ()


@dataclass(frozen=True, slots=True)
class SettingsUpdate:
    """A partial change: None leaves a setting as it is. maintenance_model "" clears it.
    budget replaces the whole budget. modules maps kinds to installed ids. api_key is stored
    for the chosen Core (modules["core"] in this update, else the saved choice) and never
    returned. mcp_servers, like modules and api_key, applies at the next start."""

    modules: Mapping[str, str] | None = None
    api_key: str | None = None
    model: str | None = None
    maintenance_model: str | None = None
    budget: Budget | None = None
    permissions: PermissionPolicy | None = None
    system_prompt: str | None = None
    work_folder: str | None = None
    mcp_servers: Sequence[McpServer] | None = None  # replaces the whole list


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Everything a late-attaching frontend needs. Recovery order: subscribe to
    events(), take snapshot(), drop envelopes with seq <= snapshot.seq, load history()
    and de-duplicate by Message.id."""

    seq: int  # last event already reflected here
    state: AgentState
    conversation: ConversationInfo | None  # None before start()
    config: AgentConfig | None  # None before start()
    model: str | None
    usage: UsageSnapshot
    operations: Sequence[OperationInfo]  # every running call, foreground included
    pending_permissions: Sequence[PermissionRequest]
    queued_messages: Sequence[MessageQueued]
    duty_wake_at: float | None = None  # see DutyScheduled
    proposals: Sequence[Proposal] = ()  # waiting for the user
    help_needed: str | None = None  # see HelpNeeded


# Runtime events. TextDelta / ReasoningDelta / ToolCallDelta are reused from Core's stream.


@dataclass(frozen=True, slots=True)
class StateChanged:
    """Emitted on any change of (state, pending_ops), so frontends see the background
    count move while IDLE; consecutive StateChanged(IDLE) events are not separate cycles.
    pending_ops is the number of running calls, foreground and background."""

    state: AgentState
    pending_ops: int = 0


@dataclass(frozen=True, slots=True)
class MessageQueued:
    """User message accepted mid-cycle or while stopping; delivered at the next turn
    boundary (or starts the next cycle). The committed Message reuses message_id."""

    message_id: str
    text: str


@dataclass(frozen=True, slots=True)
class MessageCommitted:
    """Emitted for every message appended to the transcript, of any role."""

    message: Message
    stop: StopReason | None = None  # set on assistant messages


@dataclass(frozen=True, slots=True)
class ToolStarted:
    call: ToolCall


@dataclass(frozen=True, slots=True)
class ToolProgress:
    call_id: CallId
    text: str


@dataclass(frozen=True, slots=True)
class ToolBackgrounded:
    call_id: CallId


@dataclass(frozen=True, slots=True)
class ToolFinished:
    """Final outcome. For foreground calls it is emitted right after the turn's TOOL
    message is committed (backgrounding bounds that wait). Not always preceded by
    ToolStarted (refused calls). For a
    backgrounded call it fires at termination, not for the placeholder. Every refused
    call gets a ToolResult(REFUSED) and a ToolFinished. A late
    completion of an ABANDONED call fires it again for the log only; nothing is
    appended."""

    result: ToolResult
    duration_s: float


@dataclass(frozen=True, slots=True)
class PermissionRequested:
    request: PermissionRequest


@dataclass(frozen=True, slots=True)
class PermissionResolved:
    """Also emitted for runtime-side outcomes, each with a fresh request_id: an automatic
    allow (by policy or review) is ALLOW_ONCE, a call covered by a grant is ALLOW_CONVERSATION, the DENY
    policy, a non-interactive refusal and Stop are DENY. call_id says which call it
    concerned."""

    request_id: str
    decision: Decision
    call_id: CallId


@dataclass(frozen=True, slots=True)
class ModelRetry:
    """A model attempt failed and will be retried. Frontends discard TextDelta,
    ReasoningDelta and ToolCallDelta received since the last MessageCommitted.
    `attempt` is the 1-based number of the attempt that failed."""

    attempt: int
    max_attempts: int
    delay_s: float
    error: str


@dataclass(frozen=True, slots=True)
class ModelSelected:
    model_id: str


@dataclass(frozen=True, slots=True)
class SettingsChanged:
    settings: Settings


@dataclass(frozen=True, slots=True)
class ConversationUpdated:
    """A conversation's metadata changed: title (by the user or an automatic title) or
    archived. Emitted for any conversation, open or not."""

    info: ConversationInfo


@dataclass(frozen=True, slots=True)
class ConversationDeleted:
    conversation_id: ConversationId


@dataclass(frozen=True, slots=True)
class MemoriesChanged:
    """Long-term memory notes were added, edited or deleted; re-read notes(). `saved` names
    the notes Memory saved on its own after an answer, for frontends to mention ("Memory
    updated"); it is empty for changes the user or a tool made."""

    saved: Sequence[str] = ()

@dataclass(frozen=True, slots=True)
class DutyScheduled:
    """When the open conversation's duty wakes next by itself (Unix time). None: it does
    not, because the agent is working, has no duty, or the duty is paused (Stop, a failure,
    a restart, no spending limit) until a person sends a message or activates it."""

    wake_at: float | None


@dataclass(frozen=True, slots=True)
class Proposal:
    """A tool call the agent proposed (the propose built-in, or an option of wrap_up). Nothing
    runs until the user approves it (RuntimeAPI.resolve_proposal); then exactly `call` runs,
    ungated, as the operation `id` (the propose or wrap_up call's id), and its result wakes
    the agent like any backgrounded call. A pending proposal is left out of the wake set.
    op_cancel, Stop and a restart end it; a user message also ends a card proposal, without
    waking the agent on its own."""

    id: CallId
    call: ToolCall
    reason: str
    card: bool = False  # an option of the wrap_up call `id`: shown on its card, not alone


@dataclass(frozen=True, slots=True)
class ProposalMade:
    proposal: Proposal


@dataclass(frozen=True, slots=True)
class ProposalResolved:
    """accepted: True approved, False dismissed, None withdrawn (op_cancel, Stop)."""

    proposal_id: CallId
    accepted: bool | None


@dataclass(frozen=True, slots=True)
class HelpNeeded:
    """The agent asked its supervisor (the user, or whoever drives this instance) for help
    with ask_for_help: the duty's check-ins pause until a message, an activation or a change
    of duty. None: the question is answered or dropped. Not kept across a restart, which
    pauses the duty anyway."""

    question: str | None


@dataclass(frozen=True, slots=True)
class MaintenanceStarted:
    """Emitted when Memory first uses inference during maintain()."""

    conversation_id: ConversationId


@dataclass(frozen=True, slots=True)
class MaintenanceFinished:
    conversation_id: ConversationId


@dataclass(frozen=True, slots=True)
class CompactionStarted:
    """Memory started summarizing older messages of the conversation (a Completion call
    with compacting=True)."""

    conversation_id: ConversationId


@dataclass(frozen=True, slots=True)
class CompactionFinished:
    """Follows CompactionStarted when maintain returns. compacted: maintain succeeded, said
    it acted, and the summary it asked for was not empty."""

    conversation_id: ConversationId
    compacted: bool


@dataclass(frozen=True, slots=True)
class StopStalled:
    """Stop requested but these calls didn't confirm cancellation within the grace
    period; they are abandoned and the agent moves on to STOPPED. A single call
    abandoned through cancel_operation is reported by ToolFinished(ABANDONED) only."""

    call_ids: Sequence[CallId]


@dataclass(frozen=True, slots=True)
class UsageUpdated:
    """Emitted after each charge and each tool-call reservation; elapsed time alone does
    not trigger one."""

    snapshot: UsageSnapshot


@dataclass(frozen=True, slots=True)
class Failure:
    kind: FailureKind
    message: str


@dataclass(frozen=True, slots=True)
class ShuttingDown:
    reason: str


type RuntimeEvent = (
    StateChanged
    | TextDelta
    | ReasoningDelta
    | ToolCallDelta
    | MessageQueued
    | MessageCommitted
    | ToolStarted
    | ToolProgress
    | ToolBackgrounded
    | ToolFinished
    | PermissionRequested
    | PermissionResolved
    | ModelRetry
    | ModelSelected
    | SettingsChanged
    | ConversationUpdated
    | ConversationDeleted
    | MemoriesChanged
    | DutyScheduled
    | HelpNeeded
    | ProposalMade
    | ProposalResolved
    | MaintenanceStarted
    | MaintenanceFinished
    | CompactionStarted
    | CompactionFinished
    | StopStalled
    | UsageUpdated
    | Failure
    | ShuttingDown
)


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """seq is gapless per instance. Subscriber queues are bounded and drop the oldest
    envelope on overflow; a jump in seq tells the subscriber to recover."""

    seq: int
    event: RuntimeEvent


class RuntimeAPI(Protocol):
    """What a Frontend can do. Implemented by the runtime, never by modules.

    Commands by state ("-" raises RuntimeStateError, "q" queues, "ok" allowed):

                        start  send  activate  stop   select_model  resolve_permission
      not started       ok     -     -         no-op  ok            -
      IDLE              ok*    ok    ok**      no-op  ok            -
      THINKING/TOOLS    -      q     -         ok     ok            -
      AWAITING_PERM.    -      q     -         ok     ok            ok
      STOPPING          -      q     -         no-op  ok            -
      STOPPED           ok*    ok    ok        no-op  ok            -
      FAILED            ok*    ok    ok***     no-op  ok            -

      *   only with no outstanding operations. One agent per process.
      **  this also drops a pending wake-up.
      *** not while FAILED because a hard budget is exhausted (send raises there too).

    resolve_permission is first-wins; an unknown or already-resolved id raises
    RuntimeStateError. cancel_operation is valid in any state while the call is running
    and raises RuntimeStateError otherwise.
    stop is a no-op in IDLE, STOPPED and FAILED unless operations are outstanding, in
    which case it performs a full Stop.

    Conversation and memory management (rename_conversation, archive_conversation,
    delete_conversation, notes, save_note, delete_note) is allowed in any state.
    Changes apply from the next turn or assemble. Each emits ConversationUpdated,
    ConversationDeleted or MemoriesChanged and is logged without any text. The memory
    commands raise NotImplementedError ("this memory module cannot ...") when the Memory
    module does not support them; frontends then hide those controls.

    start() leaves the agent IDLE. A resumed conversation has no pending
    wake-up, so it never runs before the user sends a message or activates it. The first
    command that reads or changes stored data (start(), list_conversations(), history(),
    deleting, ...) takes the data dir lock, held until shutdown; InstanceLockedError when
    another process holds it. send() in IDLE authorizes a
    cycle and returns at once (the state is THINKING on return, so a second send
    queues); the message is appended at the start of that cycle, so it never lands
    between an assistant message and its TOOL message. Background results that arrive
    while FAILED are appended immediately but do not wake the agent. After ShuttingDown
    nothing more is appended or emitted: a late completion of an abandoned call is
    dropped.
    """

    # Observation

    def events(self) -> AsyncIterator[EventEnvelope]:
        """Independent subscription, registered when this method is called (not on the
        first __anext__). Deltas are not replayed; see RuntimeSnapshot for the recovery
        order. The stream ends after ShuttingDown."""

    def snapshot(self) -> RuntimeSnapshot: ...
    async def list_models(self) -> Sequence[ModelInfo]: ...
    async def list_tools(self) -> Sequence[ToolSpec]: ...
    async def list_conversations(
        self, *, include_archived: bool = False
    ) -> Sequence[ConversationInfo]: ...
    async def history(
        self,
        conversation_id: ConversationId,
        *,
        before: str | None = None,
        limit: int | None = None,
    ) -> Sequence[Message]: ...
    async def search_archive(
        self,
        query: str,
        *,
        conversation_id: ConversationId | None = None,
        limit: int = 10,
    ) -> Sequence[ArchiveHit]:
        """conversation_id None searches every conversation, archived ones included."""

    async def inspect_memory(self, conversation_id: ConversationId) -> MemoryView | None: ...
    async def notes(self) -> Sequence[MemoryNote]:
        """The open conversation's long-term memory (Memory.notes, its work folder)."""

    async def skills(self) -> Sequence[Skill]:
        """The skills the open conversation has, by name; without one, those of no
        project. A frontend with commands of its own sends /name as a message when name
        is one of them."""

    # Control

    async def start(self, options: StartOptions) -> ConversationInfo: ...

    async def select_model(self, model_id: str) -> None:
        """Validated against list_models(); ValueError if unknown. Applies from the next
        inference and emits ModelSelected."""

    async def settings(self) -> Settings: ...

    async def update_settings(self, update: SettingsUpdate) -> Settings:
        """Validate and apply a partial change, save it to <data dir>/settings.toml and
        emit SettingsChanged (and ModelSelected when the model changes). Nothing is applied
        if any part is invalid (ValueError).

        Model, maintenance model, budget, permissions and system prompt apply in any state,
        from the next inference; the work folder from the next new conversation.
        Permissions are clamped to any inherited ceiling. Module settings (modules,
        api_key, mcp_servers) are saved in any state and build nothing:
        they apply at the next start. A Core change also clears the saved model and
        maintenance model, so the new Core starts on its first model. Keys go to the OS
        keychain under key_name, never to a file.

        Frontends must ask the user to confirm before loosening permissions or raising a
        budget limit. A Memory change makes another archive current, so the conversations
        of the old one are not shown after the restart; frontends meant for non-technical
        users do not offer it. Every change is written to the event log,
        without key values."""

    async def restart(self) -> None:
        """Shut down as on quit, then start again with the saved settings and the frontend
        Settings.restart_frontend names, reopening the open conversation unless the Memory
        module changes. The event stream ends, so the frontend's run() returns."""

    async def send(self, text: str) -> None:
        """Submit a user message and authorize a cycle. While a cycle runs or the agent
        is stopping, the message is queued (MessageQueued) and delivered at the next
        turn boundary; a turn that ended without tool calls starts the next cycle."""

    async def activate(self) -> None:
        """User-initiated cycle with no new message. If the transcript is empty or its
        newest message is an assistant message, the runtime first appends a short runtime
        notice as a USER message, so requests never end on an assistant message."""

    async def stop(self) -> None:
        """Idempotent; returns immediately. Cancels inference, maintenance and running
        calls, denies pending prompts, blocks wake-ups. After the grace period, calls
        that did not confirm cancellation are abandoned (StopStalled) and the state
        becomes STOPPED. Partial assistant text already streamed is committed (text
        only); reasoning and incomplete tool calls are dropped."""

    async def cancel_operation(self, call_id: CallId) -> None:
        """User-initiated cancel of one running call, with Stop's grace and abandon rules."""

    async def rename_conversation(self, conversation_id: ConversationId, title: str) -> None:
        """An empty (or whitespace-only) title clears it, so an automatic title follows."""

    async def archive_conversation(
        self, conversation_id: ConversationId, archived: bool
    ) -> None:
        """Hide (or show again) a conversation in list_conversations() and
        recall(scope="all"). The open conversation stays open."""

    async def delete_conversation(self, conversation_id: ConversationId) -> None:
        """Erase a conversation for good (see Memory.delete_conversation).
        RuntimeStateError for the open conversation; switch away first. Frontends must
        ask the user to confirm and say it cannot be undone."""

    async def save_note(self, title: str, description: str, text: str, *,
                        note_id: str | None = None) -> MemoryNote:
        """Write a note as the user (see Memory.save_note)."""

    async def delete_note(self, note_id: str) -> None:
        """Erase the note with its revisions. The messages it came from stay until their
        conversation is deleted."""

    async def resolve_proposal(self, proposal_id: CallId, accept: bool) -> None:
        """Approve or dismiss a pending proposal, in any state; RuntimeStateError when it is
        not pending."""

    async def set_duty(self, duty: Duty | None) -> None:
        """Give the open conversation a duty, change it, or end it (None); the plan stays.
        Allowed in any state after start(); a change during a cycle applies when it ends.
        Turning a duty on in IDLE, STOPPED or FAILED (not by budget) starts a cycle.
        ValueError for an empty instruction, intervals that are not 0 < min <= max, or
        no cost limit (Budget.max_cost_usd). Emits ConversationUpdated."""

    async def set_plan(self, plan: Sequence[PlanItem]) -> None:
        """Replace the open conversation's plan as the user. Emits ConversationUpdated."""

    async def resolve_permission(self, request_id: str, decision: Decision) -> None: ...


@dataclass(frozen=True, slots=True)
class FrontendInfo:
    name: str
    # False -> the runtime never sends PermissionRequested; calls that would prompt
    # are refused (see PermissionPolicy) instead of blocking on an absent user.
    interactive_permissions: bool = True
    # "terminal" (run by kayden in a terminal) or "window" (run by the window launcher,
    # which a double-clicked app starts; it needs a display).
    kind: str = "terminal"


class Frontend(abc.ABC):
    """Owns the UI. Its factory may carry `event_loop`: a callable that runs a coroutine to
    completion on the loop the frontend needs (a GUI toolkit's, for example), as asyncio.run
    does, which is the default. Launchers run each frontend that way and know no toolkit;
    after a Restart to a frontend that names another loop, the next instance runs on that
    one. Launchers may pass these factory options: start (true: start right away),
    conversation_id, model; a frontend ignores the ones it does not use."""

    @property
    @abc.abstractmethod
    def info(self) -> FrontendInfo: ...

    @abc.abstractmethod
    async def run(self, runtime: RuntimeAPI) -> None:
        """Return when the user quits; the runtime then shuts the instance down. Running
        calls end as by Stop: CANCELLED when confirmed within the grace period, ABANDONED
        otherwise; only a crash produces INTERRUPTED."""

    def tools(self) -> Sequence[Tools]:
        """Optional: Tools modules the frontend runs, such as those only an interface can
        host (kayden.frontends.sessions). Asked for once, before the runtime is built; they
        are offered after every other module's and closed with them."""
        return ()

    async def aclose(self) -> None:
        pass