# Implementation decisions

Choices the spec leaves to the implementer. Contract gaps are in `contract-issues.md`.

## Layout

`kayden.runtime` is a package:

| Module | Contents |
|--------|----------|
| `orchestrator.py` | `Runtime` (implements `RuntimeAPI`): cycle loop, tools, permissions, Stop |
| `operations.py` | `Operation`: one running call with timeouts, stall detection, abandon |
| `budget.py` | token estimator, cost, `BudgetTracker` (persisted usage) |
| `events.py` | `EventBus` (bounded subscriptions), `EventLog` (JSONL) |
| `instance.py` | `InstanceLock`, `load_module`, `run_instance` |
| `config.py` | `RuntimeConfig`, `InstanceConfig`, `ModuleSpec`, base system prompt |

## Defaults (`RuntimeConfig`)

| Setting | Default | Note |
|---------|---------|------|
| `permissions` | `ask` | clamped to any inherited ceiling |
| `initial_wait_s` | 10 s | foreground wait before a call is backgrounded |
| `default_tool_timeout_s` | 3600 s | SPEC 16.4 suggestion; `math.inf` in a ToolSpec disables |
| `cancel_grace_s` | 5 s | api.py default; also used for inference and Memory calls |
| `max_retries` | 3 | 4 attempts; `ModelRetry.max_attempts = max_retries + 1` |
| `retry_base_delay_s` / `retry_max_delay_s` | 1 s / 30 s | delay = base·2^(n-1), capped; no jitter |
| `retry_after` | honoured as given | not capped by `retry_max_delay_s` |
| `default_max_output_tokens` | 4096 | when the model doesn't state one; reserve capped at window/4 |
| `subscriber_queue_size` | 1000 | per subscriber, drop-oldest |
| `model` | first model Core lists | |
| `maintenance_model` | main model | used for `Completion(light=True)` |
| `context_windows` | {} | required for models whose window is unknown |
| Context overflow | 0.9 × assembled budget, once | |

## Token estimate

Four characters per token, plus 4 tokens per message. Tool calls count name + JSON
arguments; results count their content. On the first request for each model the runtime
calls `Core.count_tokens`; if it returns a number, the estimator is scaled by
exact/estimate, clamped to [0.25, 4].

## Files in the data directory

| File | Format |
|------|--------|
| `<mode>.lock` | `fcntl.flock` lock; contents: holder pid |
| `usage-<mode>.json` | `{"schema_version": 1, input_tokens, output_tokens, cached_tokens, cost_usd, tool_calls, elapsed_s}`; written with temp file + `os.replace` after each charge, each tool-call reservation and whenever activity drops to zero |
| `events-<mode>.jsonl` | one JSON object per line, written off the event loop by a `logging.handlers.QueueListener` thread |
| `scratch/` | `ToolContext.data_dir`, shared by all calls |

Event log records:

```json
{"ts": 1793283281.2, "seq": 12, "event": "ToolStarted", "data": {...dataclass fields...}}
{"ts": 1793283281.2, "decision": "budget_check", "before": "tool", "ok": true, "reason": null}
```

Decision kinds: `start`, `cycle_requested`, `cycle_start`, `cycle_end`, `budget_check`,
`permission`, `backgrounded`, `cancel_operation`, `abandoned`, `late_completion`, `idle`,
`stop`, `stopped`, `resume_repair`, `reserved_tool_dropped`, `maintenance_retry`,
`activation_notice`, `duration_exhausted`.
TextDelta, ReasoningDelta, ToolCallDelta and ToolProgress are not logged.

## Base system prompt

`config.DEFAULT_SYSTEM_PROMPT` explains backgrounded operations, `op_status`/`op_cancel`,
`recall` and `peek_other_mode`. Continuous Mode appends `CONTINUOUS_PROMPT` (how to idle).
Memory's `system_addendum` follows, separated by a blank line.

## Runtime-authored texts

- Placeholder (BACKGROUNDED): "Operation {id} is still running in the background (Ns so
  far). Last progress: …. Use op_status … op_cancel …; its outcome is delivered when it
  finishes."
- CANCELLED / TIMED_OUT: "Cancelled." / "Timed out." plus the last progress text.
- ABANDONED: "The call {was cancelled|timed out} but did not confirm cancellation within Ns
  and was abandoned; it may still have effects." plus the last progress text.
- INTERRUPTED (resume repair): "Interrupted: the process ended while this call was in
  flight." / "... while this operation was running."
- Activation notice (USER message, `config.ACTIVATION_NOTICE`): "[Runtime notice] You were
  activated without a new user message. Continue your work, or idle if there is nothing to
  do."
- REFUSED: names the reason (policy, denial, non-interactive frontend, or the exhausted
  budget dimension).
- recall / peek_other_mode: one line per hit, `[conversation/message role ISO-time] text`,
  text capped at 1000 characters.

## Libraries

- `jsonschema` (added dependency) validates tool-call arguments against `ToolSpec.parameters`
  (`validator_for` the schema's draft; `best_match` error reported).
- `logging` QueueHandler/QueueListener for the non-blocking JSONL log.
- `fcntl.flock` for the instance lock (POSIX).

## Module loading

`InstanceConfig` lists `ModuleSpec(factory="package.module:attr", options={...})` for each of
the four modules. `load_module` imports the attribute, calls `factory(options, paths)` with
`InstancePaths(data_dir)`, and checks the result against the module ABC. `run_instance`
builds the runtime, awaits `frontend.run(runtime)`, then shuts down (`Runtime.aclose`
cancels running calls, emits ShuttingDown, closes the modules and releases the lock).

## Test fakes

`tests/fakes/`: `ScriptedCore` (steps of text, reasoning, calls, usage reports, transient or
permanent errors, delays, hanging streams that can ignore cancellation), `FakeTools`
(instant, slow with progress, hanging with/without honouring cancellation, gated, failing,
thread-using, environment probe), `InMemoryMemory` over a `MemoryStore` that outlives
instances (idempotent append, suffix assembly with in-view truncation, compaction through
`Completion`, word-overlap archive search, per-mode long-term memory), `ScriptedFrontend`
whose `Recorder` keeps every envelope. Timing-based tests use 0.15 s initial wait and 0.2 s
grace.

## Changes for contract Draft 2.3

| # | Change | Where | Tests |
|---|--------|-------|-------|
| 1 | Barrier ordering: consecutive read-only calls overlap; a non-read-only call waits for every earlier call to finish or be backgrounded, and later calls wait for it. The barrier precedes the permission gate. | `Runtime._run_tools` | `test_barrier_*`, `test_backgrounded_call_releases_barrier`, `test_permission_allow_read_only_policy` (rewritten) |
| 2 | Duration exhaustion cancels running operations (grace, then abandon) via a watchdog task that runs while operations exist; an IDLE agent fails with `Failure(BUDGET)`. Inference keeps its own deadline. | `Runtime._duration_watchdog` | `test_duration_exhaustion_*` |
| 3 | A request that fails before its first stream event is not charged; cancellations still are. | `_Stream.started`, `Runtime._attempt` | `test_failure_before_first_event_not_charged`, `test_failed_request_charged` (adjusted) |
| 4 | At the start of each cycle, if the transcript is empty or its newest message is not USER, the runtime appends the activation notice. | `Runtime._ensure_user_last` | `test_activate_*` |
| 5 | Continuous `start()` with a different `conversation_id` raises ValueError. | `Runtime._open_conversation` | `test_continuous_single_conversation` |
| 6 | `op_status.wait_s` is capped at 60 s (schema validation rejects more; the built-in also clamps). | `_OP_STATUS_MAX_WAIT_S` | `test_op_status_wait_capped` |
| 7 | `snapshot().state` is `NOT_STARTED` before `start()`. | `Runtime._base` | command table, `test_snapshot_recovery` |
| 8 | `MessageCommitted.stop` carries the stop reason of complete assistant messages. | `Runtime._append` | `test_message_committed_carries_stop_reason` |
| 9 | `InstancePaths` imported from api.py (local copy removed); the runtime relies on `inherited_ceiling()` failing closed and on `PermissionPolicy.clamp`. | `config.py`, `instance.py`, `_effective_policy` | `test_permission_ceiling` |
| 10 | Python: `requires-python >= 3.12`, run on what `uv` provides (3.13.7). | `pyproject.toml` (unchanged) | — |

Also: `test_append_failure_stops_further_work` now expects the activation notice after the
replayed TOOL message, and `test_maintain_ordering_and_serialization` ignores tool start/end
entries in its trace (a background call may end between maintain and assemble, which was a
timing flake, not a violation).
