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
`stop`, `stopped`, `resume_repair`, `reserved_tool_dropped`, `maintenance_retry`.
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
