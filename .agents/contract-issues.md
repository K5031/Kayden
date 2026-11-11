# Contract issues

Places where `SPEC.md` or `src/kayden/api.py` is contradictory, ambiguous, or silent, found
while building the runtime from them alone. Each entry gives the location, the problem,
what the implementation assumes, and a proposed fix. Neither file was modified.

Entries are grouped by kind. **(C)** = contradiction, **(A)** = ambiguity, **(S)** = silence.

Each entry has a status against contract Draft 2.3: **Resolved** (the spec or api.py now
settles it; "Runtime changed" when the implementation had to follow), **Accepted** (the
assumption is confirmed without a spec change), or **Open** (not addressed; the stated
assumption is still what the runtime does). Issues found while adapting to Draft 2.3 are
in section 9.

## 1. Contradictions

### 1.1 (C) ToolFinished ordering vs. one TOOL message per turn
- **Status:** **Resolved** (Draft 2.3: `ToolFinished` docstring). Kept as implemented.
- **Where:** api.py docstring "each ToolResult is appended before its ToolFinished event";
  "The runtime appends exactly one [TOOL message] per turn ... with one result per call";
  SPEC 16.5.
- **Problem:** A ToolResult can only be appended once *every* call in the turn has a result,
  so a fast call's ToolFinished cannot fire when it finishes. The two rules together force
  ToolFinished for every foreground call to wait for the slowest call of the turn.
- **Assumed:** ToolFinished for foreground calls is buffered and emitted right after the
  turn's TOOL message is committed.
- **Fix:** Say so explicitly in `ToolFinished`'s docstring, or add a per-call completion
  event that is not tied to durability.

### 1.2 (C) Several queued messages vs. message-id reuse
- **Status:** **Resolved** (module docstring: one USER message each, in send order). Kept.
- **Where:** api.py "Turn boundary ... queued user messages (one USER message)";
  `MessageQueued`: "The committed Message reuses message_id".
- **Problem:** Two queued messages have two ids, but one merged USER message can carry only
  one id.
- **Assumed:** Each queued message is committed as its own USER message with its own id.
  (The boundary order already produces consecutive USER messages: op results, then queued.)
- **Fix:** "queued user messages (one USER message each, in send order)".

### 1.3 (C) Stop in IDLE is a no-op vs. Stop disabling wake-ups
- **Status:** **Resolved** (`RuntimeAPI` docstring, SPEC 5.3). Kept.
- **Where:** RuntimeAPI table, `IDLE / stop = no-op`; SPEC 5.3 "Disable automatic
  wake-ups", "Request cancellation of running tools"; `AgentState.IDLE` "background ops may
  still run".
- **Problem:** An Agent-Controlled agent that idled while a batch runs is IDLE. If Stop is a
  no-op, the user cannot prevent the autonomous wake-up or stop the batch with the one Stop
  control (only one `cancel_operation` per call).
- **Assumed:** Stop in IDLE or FAILED is a no-op only when nothing is outstanding; with
  outstanding operations it performs a full Stop (STOPPING, cancel, abandon, STOPPED, no
  wake-up). Tested by `test_stop_in_idle_with_background_ops`.
- **Fix:** Table footnote: "no-op unless operations are outstanding".

### 1.4 (C) What a failed request is charged
- **Status:** **Resolved** (SPEC 16.2 now matches api.py).
- **Where:** SPEC 16.2 "the runtime charges the last UsageReport, or an estimate";
  api.py `Core.stream`: "charges, per dimension, the larger of the last report and its own
  estimate".
- **Assumed:** api.py (the normative source): per dimension max(report, estimate).

### 1.5 (C) "Remains idle until user interaction" vs. "starts the next cycle immediately"
- **Status:** **Resolved** (2026-11-02). SPEC 5.1 now defers to 5.2, and 5.2 says when the
  rule is checked; the assumption below is the spec.
- **Where:** SPEC 5.1 "If no tools are pending, the agent remains idle until user
  interaction"; SPEC 5.2 "If results the model has not seen exist and nothing is
  outstanding, an Agent-Controlled agent starts the next cycle immediately".
- **Assumed:** Evaluated when the agent idles: an empty wake set with unseen results wakes
  immediately (Agent-Controlled); an empty wake set without unseen results waits for the
  user. Later completions of operations outside the wake set do not wake the agent.
- **Fix:** State the rule in one place, including when it is evaluated.

### 1.6 (C) "Configuration is read-only after start" vs. per-start activation
- **Status:** **Resolved** (SPEC 16.5: a later start() may change activation, never the mode). Kept.
- **Where:** SPEC 16.5; `StartOptions.activation`; table allows `start` again from IDLE,
  STOPPED, FAILED.
- **Assumed:** A later `start()` may change the activation policy (it produces a new
  `AgentConfig`); the mode cannot change.
- **Fix:** Say whether activation is part of the immutable configuration.

### 1.7 (C) Python version
- **Status:** **Accepted**: target 3.12+, run on what `uv` provides (3.13.7 here); 3.14 is a preference.
- **Where:** AGENTS.md "Python 3.14"; `pyproject.toml` `requires-python >= 3.12`; the
  provided venv runs 3.13.7.
- **Assumed:** Code targets 3.12+ (uses PEP 695 syntax as api.py does); tested on 3.13.

## 2. Execution and scheduling

### 2.1 (A) Read-only concurrency vs. emission order in mixed turns
- **Status:** **Resolved** (barrier rule, SPEC 16.4 and `Tools`). Runtime changed. See N3 for prompts.
- **Where:** SPEC 16.4, `Tools` docstring: "Read-only calls in a turn run concurrently. Other
  calls run one at a time in emission order"; SPEC 16.5 "Calls that need no prompt start
  immediately".
- **Problem:** For `[write_file, read_file]` the literal reading lets the read run
  concurrently with (or before) the write it depends on. A readers-writer ordering would be
  safer but contradicts "start immediately".
- **Assumed:** Literal: read-only calls start at once; non-read-only calls form one chain in
  emission order. Results are still reported in emission order.
- **Fix:** Decide whether a read-only call waits for preceding non-read-only calls.

### 2.2 (A) "One at a time" vs. backgrounding
- **Status:** **Resolved** ("finished or been backgrounded"). Kept.
- **Where:** same.
- **Problem:** If a non-read-only call is backgrounded, does the next one wait for it to
  *terminate* (the turn then lasts as long as the slow call, defeating backgrounding) or
  only to *leave the foreground*?
- **Assumed:** The next call starts once its predecessor has finished or been backgrounded,
  so two mutating operations can overlap.
- **Fix:** Specify.

### 2.3 (A) When the idle() wake set is computed
- **Status:** **Resolved** (wake set computed at the end of the turn; unknown ids ignored). Kept.
- **Where:** SPEC 5.2 "The wake set is every operation outstanding when idle() is called".
- **Problem:** idle() is a call within a turn; calls emitted after it in the same turn (or
  calls still waiting for permission) are not yet outstanding at that instant.
- **Assumed:** idle() takes effect at the turn boundary; the wake set is computed there.
  `wait_for` ids that are unknown or already terminated are ignored (no error).
- **Fix:** "outstanding when the turn containing idle() ends".

### 2.4 (S) idle() alongside other calls; idle() with queued messages
- **Status:** **Resolved** (2026-11-07). Stated in api.py module docstring, turn boundary.
- **Assumed:** Other calls in the turn run normally; the cycle ends after the turn. If user
  messages were queued and delivered at that boundary, the cycle continues (they authorize a
  turn), mirroring the "turn without tool calls" rule in `send`.

### 2.5 (S) Status of calls never started because of Stop
- **Status:** **Resolved** (`CANCELLED`: "or never started: Stop"). Kept.
- **Where:** `ToolStatus.REFUSED` "never started: permission denied or budget exhausted";
  `CANCELLED` "stopped by Stop ... termination confirmed".
- **Problem:** A call queued behind a sequential predecessor when Stop arrives never starts,
  for neither listed reason.
- **Assumed:** CANCELLED with content "Not started: the agent was stopped." Prompts open at
  Stop are denied, so those calls are REFUSED.
- **Fix:** Extend one of the two definitions.

### 2.6 (A) Timed-out calls that do not confirm cancellation
- **Status:** **Resolved** (`ABANDONED`: "cancel or timeout not confirmed in time"). Kept.
- **Assumed:** ABANDONED (the content says it timed out). TIMED_OUT only when cancellation
  is confirmed within the grace period.

### 2.7 (S) A tool that returns normally after cancellation was requested
- **Status:** **Resolved** (2026-11-07). Stated in `Tools` docstring.
- **Assumed:** Its real OK/ERROR result is recorded (the effect happened), not CANCELLED.

### 2.8 (S) Built-in tool execution
- **Status:** Partly **Resolved**: `wait_s` is clamped to 60 s; since Draft 2.4 the schema accepts any non-negative value and the runtime clamps (see N1). Runtime changed. Built-in timeouts and backgrounding remain open.
- **Problem:** Unspecified whether built-ins get the initial wait, backgrounding, timeouts.
  `op_status(wait_s)` has no upper bound, so the model can block its own turn for hours.
- **Assumed:** Built-ins run inline, are never backgrounded or timed out, emit ToolStarted
  and ToolFinished, and are cancelled by Stop. `wait_s` is not capped.
- **Fix:** Cap `wait_s` (e.g. at the initial wait) or define built-in timeouts.

### 2.9 (S) op_cancel semantics
- **Status:** **Resolved** (2026-11-07). Stated in `BUILTIN_TOOLS` comment.
- **Assumed:** Waits for termination (bounded by the grace period) and reports the final
  status; unknown or not-running ids return ERROR. The operation's outcome still arrives as
  an OperationResult.

### 2.10 (S) op_status after a restart
- **Status:** **Resolved** (2026-11-07). Stated in `BUILTIN_TOOLS` comment.
- **Assumed:** Operations from before a restart are unknown to op_status (ERROR); their
  outcome is the INTERRUPTED OperationResult from resume repair.

### 2.11 (S) Abandonment through cancel_operation
- **Status:** **Resolved** (`StopStalled` docstring). Kept.
- **Where:** `cancel_operation` "with Stop's grace and abandon rules"; `StopStalled` is
  defined only for Stop.
- **Assumed:** A single-call abandon is reported by `ToolFinished(ABANDONED)` only; no
  StopStalled.
- **Fix:** Say which event reports it.

### 2.12 (S) In which states cancel_operation is valid
- **Status:** **Resolved** (2026-11-07). Stated in `RuntimeAPI` docstring.
- **Where:** not in the command table.
- **Assumed:** Any state, as long as the call is running (RuntimeStateError otherwise).

### 2.13 (S) Validation "known name"
- **Status:** **Resolved** (2026-11-07). Stated in api.py module docstring, validation.
- **Assumed:** Known = offered in this turn's request: built-ins for the mode plus the
  module tools listed this turn. `idle` in Session Mode is therefore an unknown tool (ERROR).
  Tools.list_tools is called once per turn ("each cycle" read as "each request").

### 2.14 (S) Module tools redeclaring reserved names
- **Status:** **Resolved** (2026-11-07). Stated in api.py module docstring, validation.
- **Where:** SPEC 16.4 "cannot be redeclared by modules".
- **Assumed:** Dropped from the offered set and logged; no Failure event.

### 2.15 (S) Tools.list_tools failure
- **Status:** **Resolved** (2026-11-07). Stated in api.py module docstring, validation.
- **Assumed:** Failure(INTERNAL) and the turn proceeds with built-ins only.

### 2.16 (S) Built-in output size
- **Status:** **Resolved** (2026-11-07). Stated in `BUILTIN_TOOLS` comment.
- **Where:** `Tools` "the runtime does not truncate [results]".
- **Assumed:** That applies to module tools. recall/peek_other_mode format hits as text and
  cap each hit's preview at 1000 characters.

### 2.17 (S) Duplicate call ids in one turn, colliding message ids
- **Status:** **Resolved** (2026-11-07). Stated in api.py module docstring, durability order.
- **Assumed:** Not handled specially; a message-id collision surfaces as an append failure.

## 3. Idle, activation, Stop

### 3.1 (A) Session Mode and activate()
- **Status:** **Resolved** (`RuntimeAPI`: a Session agent counts as User-Controlled). Kept.
- **Where:** table footnote ** "only for a User-Controlled agent"; `StartOptions.activation`
  "ignored in Session Mode".
- **Assumed:** A session agent counts as user-controlled: `activate()` is allowed in IDLE,
  and `AgentConfig.activation` reports USER_CONTROLLED for Session Mode.

### 3.2 (S) Messages queued before Stop
- **Status:** **Resolved** (SPEC 5.3: recorded but start nothing). Kept.
- **Where:** SPEC 5.3 "Messages sent while stopping are queued and start a cycle once
  stopped".
- **Problem:** Silent about messages queued during the cycle *before* Stop, and about a
  message sent in IDLE whose cycle had not begun when Stop arrived.
- **Assumed:** They are appended once stopped but start nothing (Stop wins); only messages
  sent while STOPPING start a cycle. Tested by `test_stop_keeps_message_queued_before_stop`,
  `test_stop_before_cycle_starts`.

### 3.3 (S) Initial state at start()
- **Status:** **Resolved** (2026-11-07). Stated in `RuntimeAPI` docstring.
- **Assumed:** Session: IDLE. Continuous with a newly created conversation: IDLE.
  Continuous resuming its conversation (restart, or a second start() in the same process):
  STOPPED, whatever the activation policy (SPEC 11).

### 3.4 (S) Continuous start() with a conversation_id
- **Status:** **Resolved** (`StartOptions`: a different id raises ValueError). Runtime changed. An id given before the conversation exists also raises, since it cannot match.
- **Assumed:** Ignored; the single continuous conversation is used.

### 3.5 (S) Background completions while FAILED
- **Status:** **Resolved** (2026-11-07). Stated in `RuntimeAPI` docstring.
- **Assumed:** Results are appended immediately; no wake-up from FAILED.

### 3.6 (S) Hung inference or Memory call during Stop
- **Status:** **Resolved** (2026-11-07). Stated in api.py module docstring, Memory calls.
- **Where:** SPEC 5.3 "report the problem instead of hanging silently" (stated for
  operations).
- **Assumed:** Inference, maintain and built-in module calls get the same grace period;
  if they don't stop, Failure(INTERNAL) is emitted and Stop proceeds without them.

### 3.7 (S) send() in IDLE: when is the message persisted?
- **Status:** **Resolved** (2026-11-07). Stated in `RuntimeAPI` docstring.
- **Assumed:** `send()` authorizes a cycle and returns; the message is appended at the start
  of that cycle (before MessageCommitted, as required). This prevents a user message from
  landing between an assistant message and its TOOL message when an autonomous wake-up
  races with send. The state becomes THINKING synchronously, so a second send queues.

### 3.8 (S) Activation with nothing new / empty transcript
- **Status:** **Resolved** (`activate()` docstring, SPEC 5.1). Runtime changed. See N5. Draft 2.4 narrows the notice to an empty transcript or one ending on an assistant message; a transcript ending on a TOOL message is sent as is.
- **Problem:** `assemble` must return a suffix "that starts at a USER message ... and always
  includes the newest message". After `activate()` the newest message may be an assistant
  message (some providers reject a request ending with it). On a brand-new continuous
  conversation with no messages, no such suffix exists at all.
- **Assumed:** Passed through as assembled; Core handles it.
- **Fix:** Define what a cycle without a new user message sends to the model.

## 4. Budgets

### 4.1 (A) What "reserved atomically" means for inference
- **Status:** **Resolved** (2026-11-04). The api.py module docstring now states the
  assumption below.
- **Where:** api.py "Budgets are reserved atomically before each inference or tool start".
- **Problem:** Inference cost is unknown in advance.
- **Assumed:** Tool start: an atomic check-all-dimensions-and-count-one-call. Inference: a
  check only (one inference runs at a time; Memory's Completion runs only inside maintain,
  which never overlaps inference).
- **Fix:** Define the reserved quantity.

### 4.2 (A) Does one exhausted dimension stop everything?
- **Status:** **Resolved** (SPEC 9, `Budget`: any exhausted dimension stops all chargeable execution). Kept.
- **Where:** SPEC 9 "Once a configured hard budget is exhausted, the runtime must prevent
  further chargeable execution".
- **Assumed:** Yes: exhausting `max_tool_calls` also refuses inference (the agent cannot even
  reply). This is literal but may surprise users.
- **Fix:** Say whether dimensions gate only the work that consumes them.

### 4.3 (S) Threshold
- **Status:** **Resolved** (2026-11-07). Stated in `Budget` docstring.
- **Assumed:** Exhausted ⇔ used ≥ limit. Mid-stream, after every event, in-flight usage is
  max(last report, estimate). A request whose Completed pushes usage over the limit is
  committed (it is no longer mid-stream); the next check refuses.

### 4.4 (S) Partial output on mid-stream budget exhaustion
- **Status:** **Resolved** (2026-11-07). Stated in `Budget` docstring.
- **Assumed:** Like Stop: streamed text is committed (text only); the agent fails with
  Failure(BUDGET).

### 4.5 (S) Duration budget and running tools
- **Status:** **Resolved** (SPEC 9, `Budget`: cancel running operations as Stop does). Runtime changed. See N4.
- **Assumed:** An exhausted duration budget cancels inference (a timer, since a hung stream
  emits no events) but does not cancel running tool operations; they keep accruing time.
- **Fix:** Specify.

### 4.6 (A) Cost budget start check
- **Status:** **Resolved** (`Budget`: start() and select_model raise for a model without prices). Kept.
- **Where:** `Budget` "start() raises if it is set and the model has none and the provider
  reports no cost".
- **Problem:** Whether the provider reports cost is unknowable before the first call.
- **Assumed:** start() (and select_model) raise ValueError for a model without input and
  output prices whenever a cost budget is set; the maintenance model is checked too.

### 4.7 (A) Usage scope with two modes in one directory
- **Status:** **Resolved** (a budget belongs to the (data dir, mode), `usage-<mode>.json`). Kept.
- **Where:** SPEC 9 "Usage is persisted in the instance data directory"; SPEC 10.4 "Two
  processes may share a directory if they run different modes".
- **Assumed:** One usage file per mode (`usage-<mode>.json`), guarded by that mode's lock,
  so the two processes never write the same file. Consequence: a directory running both
  modes has two independent budgets.
- **Fix:** Say whether a budget belongs to the directory or to the (directory, mode).

### 4.8 (S) Resetting usage
- **Status:** **Resolved** (SPEC 9: delete the usage file while no process runs that mode). Kept.
- **Where:** SPEC 9 "resets only by explicit user action"; no RuntimeAPI command exists.
- **Assumed:** Delete the usage file while no process runs that mode.
- **Fix:** Add `reset_usage()` or document the file.

### 4.9 (S) Charging transient failures
- **Status:** **Resolved** (a failure before the first stream event is not charged). Runtime changed. See N6.
- **Problem:** The estimate rule charges the full input estimate for a 429 the provider
  never billed, so a rate-limit storm consumes budget.
- **Assumed:** As specified (conservative).
- **Fix:** Let Core mark a failure as unbilled.

### 4.10 (S) Elapsed time of abandoned calls
- **Status:** **Resolved** (2026-11-07). Stated in `Budget` docstring.
- **Assumed:** Duration stops counting when a call is abandoned.

## 5. Permissions and instance safety

### 5.1 (S) PermissionResolved decisions for automatic outcomes
- **Status:** **Resolved** (2026-11-07). Stated in `PermissionResolved` docstring.
- **Assumed:** Read-only auto-allow → ALLOW_ONCE; covered by a grant → ALLOW_CONVERSATION;
  DENY policy or non-interactive refusal → DENY. Each with a fresh request id.

### 5.2 (S) Invalid KAYDEN_PERMISSION_CEILING value
- **Status:** **Resolved** (`inherited_ceiling()` returns DENY for an invalid value). Runtime changed. The runtime now relies on it.
- **Where:** `PermissionPolicy.inherited_ceiling` raises ValueError.
- **Assumed:** Fail closed: treated as DENY.

### 5.3 (A) Error type for resolve_permission / cancel_operation
- **Status:** **Resolved** (2026-11-04). `RuntimeAPI` now names RuntimeStateError.
- **Where:** "an unknown or already-resolved id raises"; "cancel_operation raises unless the
  call is currently running".
- **Assumed:** RuntimeStateError for both, as for invalid states.

### 5.4 (S) When the lock is taken
- **Status:** **Resolved** (2026-11-07). Stated in SPEC 10.4 and `RuntimeAPI` docstring.
- **Where:** SPEC 10.4 lock per (directory, mode); the mode is known only at `start()`.
- **Assumed:** Acquired before the first Memory call (`run_instance` takes it before
  building the modules; a `Runtime` built otherwise takes it at its first command that
  reads or changes stored data), released at shutdown. `fcntl.flock` (POSIX only); the
  holder is named by the pid written into `instance.lock`. A review of #1 found that reads
  and writes before start() ran unlocked: a second process could delete a conversation
  the first had open, and migrate the store under an older Kayden.

### 5.5 (S) When the ceiling is exported
- **Status:** **Resolved** (2026-11-07). Stated in `PermissionPolicy.inherited_ceiling`.
- **Assumed:** At Runtime construction (process start), before any tool can run.

## 6. Events, snapshot, log

### 6.1 (C) Snapshot before start()
- **Status:** **Resolved** (`AgentState.NOT_STARTED`). Runtime changed.
- **Where:** `RuntimeSnapshot.state: AgentState`; the table has a "not started" row but no
  such state.
- **Assumed:** `state` is None before start().
- **Fix:** `AgentState | None` or a NOT_STARTED member.

### 6.2 (S) StateChanged when only pending_ops changes
- **Status:** **Resolved** (2026-11-07). Stated in `StateChanged` docstring.
- **Assumed:** Emitted on any change of (state, pending_ops), so frontends see the
  background count move while IDLE. Consumers must not count StateChanged(IDLE) events as
  cycles (this cost a test).
- **Fix:** Document, or add a separate event for the count.

### 6.3 (S) pending_ops meaning
- **Status:** **Resolved** (2026-11-07). Stated in `StateChanged` docstring.
- **Assumed:** Number of running calls, foreground and background.

### 6.4 (S) Event log contents and file
- **Status:** **Resolved** (2026-11-07). Stated in SPEC 16.6.
- **Assumed:** `events-<mode>.jsonl`; every event except deltas and progress, plus decision
  records (budget checks, permission outcomes, stop, backgrounding, abandonment, late
  completions, retries, idle/wake, cycle start/end, resume repair). See decisions.md.

### 6.5 (S) REFUSAL "shown distinctly"
- **Status:** **Resolved** (`MessageCommitted.stop`). Runtime changed. Set on complete assistant messages; None on partial text committed by Stop or budget exhaustion (no StopReason fits).
- **Where:** `StopReason.REFUSAL` "treated like END_TURN, shown distinctly".
- **Problem:** No runtime event carries the stop reason, so frontends cannot show it.
- **Fix:** Add `stop` to MessageCommitted or a dedicated event.

### 6.6 (S) UsageUpdated cadence
- **Status:** **Resolved** (2026-11-07). Stated in `UsageUpdated` docstring.
- **Assumed:** After each charge and each tool-call reservation; not continuously for
  elapsed time.

### 6.7 (S) Events after ShuttingDown
- **Status:** **Resolved** (2026-11-07). Stated in `RuntimeAPI` docstring.
- **Assumed:** A late completion of an abandoned call after shutdown is dropped.

## 7. Memory interaction

### 7.1 (S) maintain() on the first turn of a cycle
- **Status:** **Resolved** (2026-11-07). Stated in api.py module docstring, Memory calls.
- **Where:** "maintain runs after a turn's results are appended and before the next
  assemble".
- **Assumed:** Called before every inference, including the first of a cycle (after the
  user message is appended). Memory decides whether to act.

### 7.2 (S) ContextRequest budget composition
- **Status:** **Resolved** (2026-11-07). Stated in `ContextRequest.max_tokens`.
- **Where:** `ContextRequest.max_tokens` "window minus tools/output reserve".
- **Assumed:** The base system prompt is subtracted too. Output reserve =
  `max_output_tokens` of the model or 4096, capped at a quarter of the window; it is also
  sent as `InferenceRequest.max_output_tokens`.

### 7.3 (S) The "10% smaller budget" on ContextOverflowError
- **Status:** **Resolved** (2026-11-07). Stated in SPEC 16.2 and `ContextOverflowError`.
- **Assumed:** 90% of the assembled-context budget (not of the window). The overflow retry
  emits ModelRetry (delay 0) and is counted separately from transient retries.

### 7.4 (S) Completion behavior
- **Status:** **Resolved** (2026-11-07). Stated in `Completion` docstring and api.py module docstring.
- **Assumed:** Transient errors are retried with the same backoff but emit no ModelRetry
  (nothing was streamed to frontends); MaintenanceStarted on first use, MaintenanceFinished
  after maintain returns. The request has no tools. A maintain failure is Failure(MODEL)
  for CoreError, Failure(INTERNAL) otherwise; BudgetExhausted yields no maintain Failure, the
  budget re-check reports it.

### 7.5 (S) Append failure details
- **Status:** **Resolved** (2026-11-07). Stated in api.py module docstring, durability order.
- **Problem:** "starts nothing further until an append succeeds" leaves open what happens
  to an assistant message that failed to persist, and to the agent's state.
- **Assumed:** Failed messages stay in an in-memory backlog, replayed (idempotently) before
  any later append. If the assistant message failed, a TOOL message with ERROR "Not run"
  results for its calls is queued behind it so the replayed transcript stays well formed.
  The agent becomes FAILED(INTERNAL); `activate()` retries. Outside a cycle (a background
  result) a failed append also sets FAILED.

### 7.6 (S) Resume repair placement
- **Status:** **Resolved** (2026-11-07). Stated in api.py module docstring, resume repair.
- **Assumed:** Missing ToolResults are appended as one TOOL message at the tail, then the
  OperationResults as one USER message. Under the durability rules orphans can only be in
  the last assistant message; a corrupted archive with orphans earlier would break the
  "directly after" convention.

### 7.7 (S) Two processes sharing one Memory store
- **Status:** **Resolved** (`Memory` docstring: the store must tolerate concurrent readers).
- **Where:** SPEC 4.4 "An instance keeps a separate store per mode"; 10.4 lets two modes
  share a directory; `Memory` serves both modes through one module.
- **Problem:** Unspecified whether Memory must tolerate concurrent access from two
  processes (one per mode) to the same directory, which peek_other_mode relies on.

## 8. Modules and configuration

### 8.1 (S) Factory signature and instance paths
- **Status:** **Resolved** (`InstancePaths` in api.py). Runtime changed. The runtime's own copy was removed.
- **Where:** SPEC 16.1 "The factory receives its configuration and the instance paths".
- **Assumed:** `factory(options: dict, paths: InstancePaths) -> Module`, with
  `InstancePaths(data_dir)`. The result is type-checked against the module ABC.

### 8.2 (S) One module per kind
- **Status:** **Resolved** (2026-11-07). Stated in SPEC 16.1.
- **Assumed:** Exactly one Core, Memory, Tools, Frontend per instance; combining several
  tool providers is the Tools module's job.

### 8.3 (S) Where runtime settings live
- **Status:** **Resolved** (implementation): `RuntimeConfig`, with model, maintenance
  model, permissions and budget read from `[runtime]` in the instance config file
  (decisions.md, "Running Kayden").
- **Problem:** Retries, timeouts, maintenance model, context-window overrides, budgets and
  the policy are "instance configuration" but have no defined type.
- **Assumed:** `RuntimeConfig` (see decisions.md).

### 8.4 (S) Queued messages at shutdown
- **Status:** **Resolved** (2026-11-07). Stated in SPEC 16.5 (appended at graceful shutdown).
- **Where:** SPEC 16.5 "Queued messages are held in memory and lost on a crash".
- **Assumed:** On graceful shutdown they are appended, not lost.

### 8.5 (C) Graceful shutdown statuses
- **Status:** **Resolved** (2026-11-02). `Frontend.run` now states the Stop rule;
  `test_shutdown_abandons_calls_that_ignore_cancel` covers the ABANDONED case.
- **Where:** `Frontend.run` "running calls are CANCELLED".
- **Assumed:** CANCELLED when confirmed, ABANDONED otherwise (same grace rule as Stop).

## 9. Found while adapting to Draft 2.3

### N1 (C) op_status: clamp vs. schema maximum
- **Status:** **Resolved** (Draft 2.4: schema has no maximum; values above 60 are treated as 60). Runtime changed: `wait_s: 61` is now clamped, not rejected (`test_op_status_wait_clamped`).
- **Where:** .agents/process/RUNTIME_FOLLOWUP.md "Clamp `wait_s` to 60 s"; `BUILTIN_TOOLS["op_status"]` schema
  `"maximum": 60`; SPEC 16.2 (arguments are validated against the schema).
- **Problem:** Validation rejects `wait_s > 60` before the tool runs, so a clamp can never
  apply.
- **Assumed:** Validation wins: `wait_s: 61` gets an ERROR result naming the maximum (tested by
  `test_op_status_wait_capped`). The built-in still clamps defensively.
- **Fix:** Either drop `maximum` from the schema and document clamping, or say "rejected".

### N2 (A) Stale `ToolSpec.read_only` comment
- **Status:** **Resolved** (Draft 2.4 uses the proposed wording).
- **Where:** api.py `ToolSpec.read_only`: "read-only calls run concurrently; others run in order".
- **Fix:** "consecutive read-only calls run concurrently; others are barriers".

### N3 (A) Barrier vs. permission prompts
- **Status:** **Resolved** (Draft 2.4, SPEC 16.5: a barrier call's prompt opens when its turn comes). Matches the assumption; no runtime change.
- **Where:** SPEC 16.4 barrier rule; SPEC 16.5 "Calls that need no prompt start immediately".
- **Problem:** Unclear whether a waiting non-read-only call may prompt before the barrier
  opens, and "start immediately" no longer holds for a read after a write.
- **Assumed:** The barrier comes first: a call waits for its predecessors, then goes through
  the permission gate, then starts. Prompts therefore open one at a time in emission order;
  during a prompt only backgrounded calls (and earlier-turn operations) keep running.
- **Fix:** "Calls that need no prompt start as soon as the barrier rule allows."

### N4 (S) Agent state after duration exhaustion cancels operations
- **Status:** Partly **Resolved** (Draft 2.4, SPEC 9: the agent fails with a budget error once the operations have terminated or been abandoned). Matches the assumption for the final state; STOPPING/StopStalled remain unspecified.
- **Where:** SPEC 9 "An exhausted duration budget also cancels running operations as Stop does".
- **Problem:** Silent about state: does the agent pass through STOPPING, is StopStalled
  emitted, what happens to an IDLE agent with only background operations?
- **Assumed:** No STOPPING and no StopStalled (it is not a user Stop); abandonment is reported
  by `ToolFinished(ABANDONED)`. Inside a cycle the next budget check fails the agent with
  `Failure(BUDGET)`. An IDLE agent fails with `Failure(BUDGET)` at once; the cancelled
  outcomes are still appended as OperationResults.
- **Fix:** State the resulting state and events.

### N5 (A) Which messages trigger the activation notice
- **Status:** **Resolved** (Draft 2.4: only an empty transcript or one ending on an assistant message gets the notice). Runtime changed: the assumption below no longer holds; a TOOL message is sent as is.
- **Where:** `activate()` "If the newest message is not a user message".
- **Assumed:** Literal: a TOOL message is not a user message, so activating after Stop (which
  ends on a TOOL message) or after an append-failure replay adds the notice. The check runs
  at the start of every cycle; send() and wake-ups always end on a USER message, so in
  practice only activate() adds it.

### N6 (A) Cancelled before the first stream event
- **Status:** **Resolved** (2026-11-04). `Core.stream` now states the assumption below;
  `test_cancel_before_first_event_charges_the_input_estimate` covers it.
- **Where:** api.py `Core.stream` "a failure before the first stream event is not charged".
- **Assumed:** Only failures (exceptions) are unbilled. A request cancelled by Stop or by the
  duration budget before any event is still charged the input estimate, since the provider
  may have started processing it.
- **Fix:** Say whether cancellation is covered.

## 10. Found while migrating the module implementations (.agents/process/MODULES.md)

Against contract Draft 2.4. The stated assumption is what the module does.

### M1 (C) Memory.assemble: "starts at a USER message" vs. "always includes the newest message"
- **Status:** **Resolved** (2026-11-07). Stated in `Memory.assemble`.
- **Where:** `Memory.assemble` docstring.
- **Problem:** A transcript (or the part after the last compaction) may contain no USER
  message at all, e.g. one written by an older runtime or by hand. Both rules cannot hold.
- **Assumed:** The suffix starts at the latest USER message that splits no call from its
  result; with none, at the latest such message of any role. Compaction only cuts before a
  USER message, so the active part of a runtime-written transcript always starts with one.
- **Fix:** "...starts at a USER message when the transcript has one".

### M2 (C) Memory.assemble: a budget too small for the newest exchange
- **Status:** **Resolved** (2026-11-07). Stated in `Memory.assemble`.
- **Where:** `Memory.assemble` "Must fit request.max_tokens" and "always includes the
  newest message".
- **Problem:** From the last USER message on, the newest exchange may hold several
  messages (tool calls, results). Truncated to their markers they can still exceed a tiny
  budget; ToolCall arguments cannot be truncated without becoming invalid.
- **Assumed:** Including the newest exchange wins: it is truncated as far as possible and
  returned even if it still exceeds the budget. The runtime then gets ContextOverflowError
  from Core, which it already handles.
- **Fix:** State the priority, or give assemble a way to report that the budget is
  infeasible.

### M3 (S) Memory.history with an unknown `before` id
- **Status:** **Resolved** (2026-11-07). Stated in `Memory.history`.
- **Assumed:** KeyError, like an unknown conversation id.

### M4 (S) Error type for a conflicting re-append
- **Status:** **Resolved** (2026-11-07). Stated in `Memory.append`.
- **Where:** `Memory.append` "the same id with different content raises".
- **Assumed:** ValueError, and the whole batch is rejected (nothing from it is appended).

### M5 (S) Truncating reasoning and tool calls in the assembled view
- **Status:** **Resolved** (2026-11-07). Stated in `Memory.assemble`.
- **Where:** `Memory.assemble` "A single oversized message is truncated in this view only".
- **Problem:** Reasoning text cannot be shortened without invalidating its provider
  signature; ToolCall arguments cannot be shortened without breaking their JSON.
- **Assumed:** In a truncated message Reasoning blocks are dropped, Text, ToolResult and
  OperationResult bodies are cut with a marker naming the message id and `recall`, and
  ToolCall blocks are kept whole.

### M6 (S) Output size of the light model
- **Status:** **Resolved** (2026-11-07). Stated in `Completion` docstring; the runtime now caps `max_tokens` (`test_completion_caps_max_tokens_at_the_model_limit`).
- **Where:** `Completion.max_input_tokens`; `Completion.__call__(max_tokens=...)`.
- **Problem:** Memory learns the light model's window but not its output limit, so it
  cannot know whether its `max_tokens` request is accepted.
- **Assumed:** Memory asks for its summary budget plus 256 tokens and sizes the input so
  that input plus that output fits the window. The runtime is expected to clamp
  `max_tokens` to the model's output limit.
- **Fix:** Have `Completion` clamp, or expose `max_output_tokens(light=...)`.

### M7 (A) Migrating an append-only archive
- **Status:** **Resolved**. There is no migration: stores use the stable `BLOCK_TYPES`
  tags from the start, and no code reads or rewrites Python import-path tags. A version 1
  store (written with import-path tags) is refused on open with a clear error ("memory
  schema version 1 is not supported (expected 2)"). The archive is never updated.
- **Where:** api.py `BLOCK_TYPES` comment, .agents/process/MODULES.md "migrate rows already written with
  import paths"; AGENTS.md "Memory archive: append-only".
- **Problem:** Rewriting stored tags is an update to an archive that must be append-only.

### M8 (S) Recognising a context overflow
- **Status:** **Resolved** (2026-11-07). Stated in `ContextOverflowError`.
- **Where:** `ContextOverflowError` "The request exceeded the model's context window".
- **Problem:** Neither API has a dedicated status or type for it. Anthropic answers 400
  `invalid_request_error` ("prompt is too long", "input length and `max_tokens` exceed
  context limit"); OpenAI answers 400 with code `context_length_exceeded`, and compatible
  servers (vLLM, llama.cpp, Ollama) only with messages that vary.
- **Assumed:** Cores match those codes and message fragments on 400/413 responses. An
  unrecognised wording stays a permanent CoreError, so the runtime's re-assembly retry
  does not happen for it.

### M9 (S) Refusal or content filter with tool calls
- **Status:** **Resolved** (2026-11-07). Stated in `StopReason.REFUSAL`.
- **Where:** `StopReason.REFUSAL` "treated like END_TURN".
- **Problem:** A turn can carry tool calls and still end with a refusal/content-filter
  reason. Treated like END_TURN, those calls would never get results.
- **Assumed:** OpenAI-compatible: a turn with calls reports TOOL_USE whatever the finish
  reason. Anthropic reports what the API says (it does not combine them in practice).

### M10 (A) Reasoning without a `model`
- **Status:** **Resolved** (2026-11-07). Stated in `Reasoning.model`.
- **Where:** `Reasoning.model` "Cores drop state from other models".
- **Problem:** Blocks persisted before the field existed have `model=None`.
- **Assumed:** Unknown counts as another model: such reasoning is not replayed (an invalid
  signature would make the request fail permanently).

### M11 (S) Refusals in history
- **Status:** **Accepted** (2026-11-07). Left as is: the reply text is saved either way, and
  only the refusal mark is lost after a restart. The fix needs a schema change (a stop
  column), which is not worth it for a display detail.
- **Where:** `MessageCommitted.stop`, `StopReason.REFUSAL` "shown distinctly".
- **Problem:** The stop reason exists only on the live event; `Message` does not carry it,
  so after a restart or a late attach (history) a refusal looks like ordinary text.
- **Assumed:** The TUI marks refusals only when it sees `MessageCommitted`.
- **Fix:** Persist the stop reason on assistant messages (e.g. `Message.stop`).

### M12 (S) Which snapshot operations are backgrounded
- **Status:** **Resolved** (2026-11-07). `OperationInfo.backgrounded`; the TUI uses it on
  attach (`test_late_attach_shows_backgrounded_calls`).
- **Where:** `RuntimeSnapshot.operations` "every running call, foreground included";
  `OperationInfo` has no flag for it.
- **Problem:** A late-attaching frontend cannot tell a backgrounded call from one still
  in the foreground wait, unless history happens to contain its BACKGROUNDED placeholder.
- **Assumed:** The TUI shows such calls as running and switches to "background" when the
  placeholder is in history or `ToolBackgrounded` arrives.
- **Fix:** Add `backgrounded: bool` to `OperationInfo`.

### M13 (S) Time-of-day prices
- **Status:** **Accepted** (2026-11-02). Left as is: costs only read high, so a cost budget
  can stop early but never overspend, and it matters only on DeepSeek.
- **Where:** `ModelInfo.input_usd_per_mtok` and siblings hold one price per model.
- **Problem:** DeepSeek's off-peak rates are half its peak rates, on a UTC schedule that
  also depends on Chinese public holidays.
- **Assumed:** The DeepSeek profile lists peak prices. Costs read up to 2x high, so
  `max_cost_usd` can stop a run early but cannot overspend.
- **Fix:** None needed for v1. Otherwise allow a Core to fill `Usage.cost_usd` with its own
  calculation (its docstring now says "when the provider doesn't report it").

### M14 (S) No maintenance marker on inference requests
- **Status:** Resolved (2026-11-02): `InferenceRequest.maintenance`. Design:
  [Agent Note](notes/implemented/2026-11-02-maintenance-marker.md).
- **Where:** `InferenceRequest` has no field for it; `Completion` has `light`, which the
  runtime does not pass on.
- **Problem:** A Core cannot tell maintenance from a turn, yet a provider may need different
  options for it (thinking off on DeepSeek).
- **Assumed (until resolved):** A request without tools is maintenance, because the runtime
  always offers built-ins on a turn.
- **Fix:** Add `InferenceRequest.maintenance: bool`.

### M15 (S) Empty compaction output
- **Status:** **Resolved** (2026-11-07). `Completion` says Memory must change nothing on an
  empty result; SqliteMemory skips the compaction and logs it
  (`test_an_empty_summary_compacts_nothing`).
- **Where:** `Completion` and `Memory.maintain`; `SqliteMemory.maintain`.
- **Problem:** A Completion may return "" (a thinking model that spends its whole token limit
  on reasoning). SqliteMemory commits that as the summary, so the messages it replaces drop
  out of the context view, and the previous summary with them. The archive keeps them.
- **Assumed:** Nothing is changed in Memory. The DeepSeek profile disables thinking on
  maintenance requests (`InferenceRequest.maintenance`, M14); any other provider that
  thinks by default stays exposed.
- **Fix:** State that empty output means no compaction, or have Completion raise CoreError.

## 11. Found while building the desktop app

### M16 (S) The Frontend does not own the main loop
- **Status:** Resolved (swappable modules).
- **Where:** `api.Frontend` docstring: "Owns the UI and the process's main loop."
- **Problem:** The launcher creates the event loop (`asyncio.run`) before any module is
  loaded, so a frontend cannot choose it. A GUI toolkit's loop must be the one that runs.
- **Resolution:** The docstring says "Owns the UI; its launcher chooses the event loop".
  `FrontendInfo.kind` (and a frontend factory's `kind`) says which launcher runs it:
  `"terminal"` frontends run under `kayden` on asyncio's loop, `"window"` frontends under
  the desktop launcher on qasync's, which `kayden` hands over to.

## 12. Found while building autonomy into sessions (contract Draft 3)

### A1 (C) "SqliteMemory schema version 2"
- **Status:** Resolved in the implementation.
- **Where:** [Agent Note](notes/implemented/2026-11-07-autonomy-into-sessions.md), "Migrating
  existing data": "schema version 2, migrated in place".
- **Problem:** Stores were already at version 2 (stable block tags); the owner's
  `~/.kayden/memory.db` is version 2, not 1.
- **Assumed:** The new layout is version 3.
- **Fix:** None needed beyond the note, which now says version 3.

### A2 (C) A restarted autonomous conversation: idle or stopped
- **Status:** Resolved (SPEC 11 and the `RuntimeAPI` docstring now say idle).
- **Where:** The note: "opens idle and never wakes before the user sends a message or
  activates it (SPEC 11's rule for continuous agents, kept)"; SPEC 11 said "comes back
  stopped".
- **Assumed:** It opens IDLE with no pending wake-up; both rules' purpose (nothing runs
  before the user acts) holds.

### A3 (S) Showing an untitled conversation
- **Status:** Resolved (`ConversationInfo.preview`).
- **Where:** The note: "frontends then show the start of the first message".
- **Problem:** No contract call returns the first message cheaply; `history` pages from
  the end.
- **Fix:** `ConversationInfo.preview`, filled by Memory.

### A4 (A) Not re-extracting a forgotten fact "from the summary it came from"
- **Status:** Accepted.
- **Where:** The note, "Long-term memory the user can see and change".
- **Problem:** A fingerprint of the text cannot tell where a new extraction came from.
- **Assumed:** A new entry whose normalized text matches a forgotten one is dropped, from
  any source; a reworded fact is accepted. Docs say a forgotten fact can come back from
  new messages.

### A5 (S) A failed title call
- **Status:** Resolved (`Memory.maintain` docstring).
- **Problem:** Whether a CoreError from the title call fails maintenance (Failure(MODEL),
  shown to the user) or is quiet.
- **Assumed:** SqliteMemory logs it and leaves the title empty; BudgetExhausted still
  propagates.

### A6 (A) Deleting a conversation "while any operation of it is outstanding"
- **Status:** Accepted.
- **Where:** The note, "Deletion".
- **Problem:** Only the open conversation can have operations (start() needs none
  outstanding), so the rule is covered by refusing the open conversation.
- **Assumed:** `delete_conversation` refuses the open conversation and nothing else.

### A7 (S) The event log keeps message text after deletion
- **Status:** Resolved. The log records events without content (SPEC 16.6).
- **Where:** SPEC 16.6 (the event log records every event except deltas and progress,
  `MessageCommitted` included) vs. the note's "deletion controls what the disk keeps".
- **Problem:** `events.jsonl` holds the full text of every committed message. Deleting a
  conversation does not touch it, and its records do not name the conversation, so it
  cannot be scrubbed per conversation.
- **Assumed:** Deletion reaches the database only; docs (`concepts.md`, "Deleting") tell
  the user the activity log keeps a record and how to remove it.
- **Fix:** Log `MessageCommitted` (and `MessageQueued`) without text, keeping ids, roles
  and block types; or write the conversation id into each record so deletion can rewrite
  the log.

### A8 (S) Emptying the write-ahead log while another process reads
- **Status:** Accepted.
- **Problem:** `wal_checkpoint(TRUNCATE)` cannot complete while another connection reads
  the store, so erased text may stay in `memory.db-wal` until the next checkpoint.
- **Assumed:** One process per data folder (the instance lock) makes this rare; the
  incomplete checkpoint is logged.

### A9 (S) Frontends cannot ask what Memory supports
- **Status:** Open.
- **Where:** `RuntimeAPI` docstring: frontends hide the memory and delete controls when the
  Memory module does not support them.
- **Problem:** The only way to find out is to call and catch NotImplementedError. The TUI
  probes `memories()` before opening its memory screen and can only disable Delete after a
  delete fails.
- **Fix:** A capability flag (on `Settings` or `RuntimeSnapshot`) saying whether memory
  editing and deletion are supported.

## Material outside the allowed inputs

None was consulted or encountered. The only out-of-tree output seen was a system-wide
`pip list` (unrelated packages) during the initial environment check.

Update for the module migration: `.agents/process/MODULES.md` (added by the user after the clean-room
build) explicitly directs importing the module implementations from
the sibling repositories `kayden-memory`, `kayden-core` and `kayden-tools`. Their
`src/` and `tests/` were copied in (read-only use of those folders); their `api.py` copies
were not, so this repository's contract stays the only one. Nothing else in those folders
was used.

## 13. Found while building swappable modules

### S1 (A) "Settings of the running modules apply live"
- **Status:** Resolved by the owner.
- **Where:** The swappable modules note (proposed) kept the API key and work folder live;
  `RuntimeAPI.update_settings` rebuilt the Core or Tools for them.
- **Problem:** Two rules for module settings, and the live rebuild with its quiescence
  checks stayed for those two.
- **Resolution:** One criterion: what the runtime reads while running applies live; what is
  only passed to a factory applies at the next start. The key and work folder are saved at
  once and apply at restart; nothing rebuilds a module while Kayden runs.

### S2 (A) `[runtime].context_windows` duplicates the Core's `context_window`
- **Status:** Resolved.
- **Where:** `RuntimeConfig.context_windows`, the Cores' `models` option.
- **Problem:** Two places configure a model's window; by the criterion above it is a module
  option.
- **Resolution:** `RuntimeConfig.context_windows` is removed. A model without a reported
  window is refused with a message pointing at the Core's `models` option.

### S3 (S) Frontend options passed by launchers
- **Status:** Resolved.
- **Where:** `Frontend` contract, launchers.
- **Problem:** Launchers pass start options (`start`, `conversation_id`, ...) to the TUI
  through its factory options; with frontends chosen by id, any frontend receives them.
- **Resolution:** The `Frontend` docstring lists the options launchers may pass; a frontend
  ignores the ones it does not use.

## 14. Found while proposing duties

### D1 (C) PHILOSOPHY.md and the spec's scope for fleets
- **Status:** Resolved (SPEC 1, 3, 4.4, 5, 10.1, 13): duties and the headless frontend.
- **Where:** `PHILOSOPHY.md` (Kayden as the base agent of a fleet manager with ranks,
  supervisors and duties) against SPEC 1 ("Kayden does not implement a bespoke multi-agent
  organization framework"), 10.2 (no centralized fleet management, no hardcoded hierarchy)
  and 13 (centralized fleet management and scheduled wake-ups out of scope).
- **Problem:** The owner's direction needs units that run by themselves, headless, and
  report upward; the spec rules out the fleet and the self-scheduling.
- **Assumed:** Both hold if the fleet manager is a separate program: the spec stays about
  one Kayden instance, and ranks and teams stay out of it. What a unit needs is added to
  the spec as it is built: duties
  ([note](notes/implemented/2026-11-11-duties.md)),
  then a headless frontend ([note](notes/implemented/2026-11-11-headless-units.md)).
- **Fix:** SPEC 1 and 13 point to PHILOSOPHY.md for the fleet and name what Kayden
  provides toward it.
