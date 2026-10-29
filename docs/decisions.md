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

## Changes for contract Draft 2.4

| Change | Where | Tests |
|--------|-------|-------|
| The activation notice is appended only when the transcript is empty or its newest message is ASSISTANT; a transcript ending on a TOOL message (e.g. after an append failure) is sent unchanged. | `Runtime._ensure_user_last` | `test_activate_after_tool_message_adds_no_notice`, `test_append_failure_stops_further_work` (updated) |
| `op_status.wait_s` above 60 is no longer rejected by validation; the built-in clamps it to `_OP_STATUS_MAX_WAIT_S` (60 s). Implementation unchanged. | `Runtime._builtin` | `test_op_status_wait_clamped` (replaces `test_op_status_wait_capped`) |

## Module migration (MODULES.md)

Module streams were imported with `rsync -a --ignore-existing`, so this repository's
`api.py`, `__init__.py` files and runtime were kept. Dependencies were merged into
`pyproject.toml`: `anthropic`, `openai`, `httpx2` (the HTTP client both SDKs depend on),
`textual`; `psutil` for tests.

### Memory (`kayden.memory_sqlite`)

- Factory: `kayden.memory_sqlite:factory`. Options: `path` (default
  `<data_dir>/memory.db`, shared by both modes of a data directory) plus any
  `CompactionConfig` field; an unknown option raises TypeError.
- `CompactionConfig` is all fractions of `ContextRequest.max_tokens`: trigger 0.75
  (summary plus active verbatim context), remove 0.5 (cut-half), summary 0.05, long-term
  rendering 0.05. All token figures use the runtime's `TokenCounter`.
- Compaction cuts only before a USER message that splits no call from its result (or
  pending call). The request to the light model is sized so that system prompt, transcript
  and requested output fit `max_input_tokens(light=True)`: the cut moves earlier if needed,
  and a single oversized exchange is shown truncated (the archive keeps it whole). If not
  even that fits, maintain does nothing and returns False.
- `assemble` priority: the newest exchange (from the last USER message on, truncated in
  view if it alone is too big), then the summary, then the newest long-term entries (at
  most 5% of the budget), then older verbatim messages as far back as fits.
- Storage format 2: blocks are `{"type": tag, "data": {...}}` with `BLOCK_TYPES` tags.
  `OpaqueBlock(type, data)` is stored as exactly that. An unknown tag, or a known tag whose
  data the current class cannot take (a field added later), decodes to `OpaqueBlock`, so it
  re-encodes unchanged. Appending a block class without a tag raises TypeError.
- Schema version 2 (`PRAGMA user_version`). Opening a version 1 store rewrites
  `kayden.api:<Class>` tags to stable tags in one transaction (dropping and re-creating the
  no-update trigger around it); other import-path tags stay and decode to `OpaqueBlock`.
  Any other version is refused.
- `append` compares a re-appended id with the stored message (decoded equality) and skips
  it when equal; a conflict raises ValueError and rolls back the batch.
- Search: FTS5 over the strings of each block minus ids and reasoning state, words ORed,
  score = −bm25 (higher is better). Unknown `conversation_id` raises KeyError.
- Concurrency: WAL journal, one connection per `SqliteMemory` on a dedicated worker
  thread; a second process opening the same file reads and writes alongside it (SQLite's
  default 5 s busy timeout). Token counting in `assemble`/`maintain` runs on the event loop
  (the counter is the runtime's fast estimate).

### Core (`kayden.core.anthropic`, `kayden.core.openai_compat`)

- Factories: `kayden.core.anthropic:factory`, `kayden.core.openai_compat:factory`; the
  options are the constructor's keyword arguments (`api_key`, `base_url`, `options`,
  `models`, ...). Keys default to `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`.
- Provider options (thinking, temperature, tool_choice, reasoning_effort, ...) are
  configuration: `options` applies to every request, `models[<id>].options` overrides it
  per model. They are merged into the request body as given, so they may also override
  `max_tokens`.
- `models[<id>]` (`ModelConfig`) also sets `context_window`, `max_output_tokens` and the
  three prices. Configured values win over provider-reported ones. With nothing
  configured, the context window is what the provider reports (Anthropic
  `max_input_tokens`; OpenAI-compatible `context_length`/`max_model_len`/`context_window`
  extras) or None.
- `count_tokens`: Anthropic uses its counting endpoint; OpenAI-compatible returns None.
  When an OpenAI-compatible server reports no usage, `Completed.usage` is a 4-chars-per-
  token estimate.
- Unparseable or non-object tool arguments give `ToolCall(arguments={}, raw_arguments=raw)`.
  When replayed, such a call is sent with `{}` arguments.
- Rendering: ToolResults with a status other than OK and ERROR are prefixed `[<status>]`
  (OpenAI-compatible also prefixes ERROR, having no error flag). Anthropic `is_error` is
  true for every status except OK and BACKGROUNDED. An OperationResult becomes the text
  `[Background operation <id> finished: <status>]` plus its content. OpaqueBlock is skipped.
- Reasoning: produced blocks carry `model=request.model`; only blocks whose `model`
  equals the request's model are replayed (Anthropic), and OpenAI-compatible never
  replays reasoning.
- Stop reasons: Anthropic `refusal` and OpenAI `content_filter` map to REFUSAL.
