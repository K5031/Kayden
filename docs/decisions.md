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

## Module migration (docs/process/MODULES.md)

Module streams were imported with `rsync -a --ignore-existing`, so this repository's
`api.py`, `__init__.py` files and runtime were kept. Dependencies were merged into
`pyproject.toml`: `anthropic`, `openai`, `httpx2` (the HTTP client both SDKs depend on),
`textual`; `psutil` for tests.

### Memory (`kayden.memory.sqlite`)

- Factory: `kayden.memory.sqlite:factory`. Options: `path` (default
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
- Schema version 2 (`PRAGMA user_version`). Stores use only the stable tags from the
  start; there is no migration and no code reads Python import-path tags. Any other
  version, including version 1 stores written by the pre-migration module with
  import-path tags, is refused rather than read as opaque blocks.
- The archive is strictly append-only: the source never runs UPDATE or DELETE on
  `messages`, and the no-update/no-delete triggers stay in place for the life of a store.
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
  `models`, ...). Keys default to `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, or the variable
  of the selected profile.
- Provider options (thinking, temperature, tool_choice, reasoning_effort, ...) are
  configuration: `options` applies to every request, `models[<id>].options` overrides it
  per model. They are merged into the request body as given, so they may also override
  `max_tokens`.
- `models[<id>]` (`ModelConfig`) also sets `context_window`, `max_output_tokens` and the
  three prices. Configured values win over provider-reported ones. With nothing
  configured, the context window is what the provider reports (Anthropic
  `max_input_tokens`; OpenAI-compatible `context_length`/`max_model_len`/`context_window`
  extras) or None. OpenAI-compatible also reads a `max_output_tokens` extra.
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
  equals the request's model are replayed (Anthropic, and OpenAI-compatible profiles with
  a `reasoning_input_field`). The stock OpenAI-compatible profile never replays reasoning.
- Stop reasons: Anthropic `refusal` and OpenAI `content_filter` map to REFUSAL.

### OpenAI-compatible profiles (`profile` option of `kayden.core.openai_compat:factory`)

Design: `design/deepseek-profile.md`.

- A `Profile` is plain data in `openai_compat.py`; generic code reads its fields and never
  a provider name. `"openai"` (default) is stock behavior. `models` entries in the
  configuration merge over the profile's field by field.
- A profile with its own `base_url` takes nothing from the OpenAI environment: no
  `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_ORG_ID` or `OPENAI_PROJECT_ID`.

**`deepseek`.** Results of the live checks of 2026-10-30, against `deepseek-flash`
(DeepSeek-V4.1-Flash-1017) and `deepseek-v4-pro`, some of which differ from DeepSeek's docs:

- Replay: omitting `reasoning_content` from an assistant tool-call turn answers 400 when
  the call id is unknown to the server. A known id is accepted and billed as if replayed, so
  the server keeps recent reasoning itself, and a test using fresh ids would miss the 400.
  The 400 also occurs on a request without `tools` while thinking is on, though the docs
  say reasoning is ignored there. It did not occur with thinking off, for a tool turn
  followed by later messages, or for text-only turns, though the docs say all turns.
- The Core therefore sends the field on every assistant message, on every request: the
  same model's reasoning, else `""`, which both models accept. Reasoning of another model
  is not replayed, in line with M10.
- A turn holding only reasoning is dropped; consecutive user messages are accepted.
- Thinking counts against `max_tokens`: at `max_tokens=20`, the answer is reasoning only
  with finish reason `length`. Hence the profile's `max_output_tokens` of 64000 (DeepSeek's
  default for thinking, against 393216 reported by `/models`). Replayed reasoning counts
  as input tokens, and the runtime's estimator counts it too.
- Requests without tools are maintenance (see M14) and run with thinking disabled, unless
  `options` says otherwise, so reasoning cannot use up a summary's token limit.
- `insufficient_system_resource` and `aborted` are transient. Neither was seen live.
- Prices are the peak rates of 2026-10-30 (see M13). They are not in `/models`.
- Overflow: 400 with code-less wording "This model's maximum context length is ...", which
  the existing message match recognises (`ContextOverflowError`).
- Cached tokens: `prompt_tokens_details.cached_tokens` equals `prompt_cache_hit_tokens`.
- Usage arrives only on the last chunk, so a cancelled stream yields no `UsageReport` and
  the runtime charges its estimate, all of it at the uncached price.
- `tool_choice` set to `required` or a function fails with 400 in thinking mode, per the
  docs (not tried live). It is configuration, so it surfaces as a permanent error.

### Tools (`kayden.tools.shell_files`)

- Factory: `kayden.tools.shell_files:factory`. Options: `root` (default: the process's
  current directory), `shell_timeout` (default None = the runtime's default; `math.inf`
  for none), `shell_stall_timeout` (default 600 s without output). File tools keep the
  runtime defaults (no stall detection, default hard timeout).
- Subprocesses get the process environment unchanged (`env` is never passed), so
  `KAYDEN_PERMISSION_CEILING` reaches nested Kayden instances; covered by the
  conformance suite's `env_probe` case.
- `list_files`/`search_files` walk the tree in a worker thread and send a silent heartbeat
  (`progress("")`) from it at most every 0.5 s. The shell never sends heartbeats: its stall
  detection is meant to catch a command that stopped producing output.
- The conformance suite's `RecordingContext` is thread-safe like the runtime's context and
  counts heartbeats separately from visible progress.

### TUI (`kayden.frontends.tui`)

- Factory: `kayden.frontends.tui:factory`. Options `mode`, `activation`,
  `conversation_id`, `model`: with a mode the agent starts at once, otherwise the start
  screen asks. `run()` returns when the user quits or `events()` ends.
- History is loaded `HISTORY_PAGE` (100) messages at a time; a "Load earlier messages"
  button at the top of the transcript prepends the previous page (`history(before=...)`).
  Late-attach recovery follows `RuntimeSnapshot`'s order and also restores queued messages.
- Per-operation cancel: Ctrl+K lists `snapshot().operations` and cancels the chosen call;
  `/cancel <call id>` does the same directly.
- Permission choices are Allow once, Allow for conversation, Deny (`/allow once|conversation`,
  `/deny`). Runtime-side decisions (policy, Stop) are noted on the call via
  `PermissionResolved.call_id`.
- Activate (Ctrl+R, `/activate`) is offered once started, except for an idle
  agent-controlled Continuous agent; the runtime remains the authority and its errors are
  shown as notifications.
- Queued messages are matched to their commit by `message_id`. A BACKGROUNDED placeholder
  keeps the call shown as running in the background until its final ToolFinished or
  OperationResult. Refusals get a red "declined" line. The model shown follows
  `ModelSelected`.

## Running Kayden

Design of the config file: `design/config-file.md`.

```
export ANTHROPIC_API_KEY=...
cd <project to work in>          # shell/file tools are rooted here
uv run --project <kayden checkout> python -m kayden [--config FILE] [--mode session|continuous]
    [--activation user_controlled|agent_controlled] [--conversation ID]
    [--model MODEL_ID] [--maintenance-model MODEL_ID]
    [--permissions ask|allow_read_only|deny] [--data-dir DIR]
```

`kayden/__main__.py` builds an `InstanceConfig`. The modules come from a TOML file:
`--config FILE` (must exist), else `<data dir>/config.toml` (optional). Sections `[core]`,
`[memory]`, `[tools]`, `[frontend]`, each with `factory` and `options`; anything else is
refused at startup. A module left out gets its default: `kayden.core.anthropic:factory`,
`kayden.memory.sqlite:factory` (`<data dir>/memory.db`), `kayden.tools.shell_files:factory`
rooted at the current directory (only when no tools options are given), and
`kayden.frontends.tui:factory`. The file is read with stdlib `tomllib`. `[runtime]` holds the
per-instance settings: `model`, `maintenance_model`, `permissions` and `[runtime.budget]`
(the `Budget` fields, non-negative). Keeping the budget limit in the data directory, next to
the usage it limits, is the point: a flag alone would let each launch pick or forget the
limit. `--model`, `--maintenance-model` and `--permissions` override the file. The other
`RuntimeConfig` fields (retries, timeouts, system prompt, ...) are tuning and stay at their
defaults until someone needs them. `--mode`, `--activation` and
`--conversation` are merged over the frontend options. The data directory defaults to
`$KAYDEN_DATA_DIR` or `~/.kayden`. Without `--mode` the TUI's start screen asks for mode,
activation, conversation and model. The launcher checks no API keys: each Core refuses a
missing key at construction (the Anthropic Core accepts `ANTHROPIC_API_KEY` or
`ANTHROPIC_AUTH_TOKEN`).

Module startup errors are one line. `load_module` turns an `ImportError` or
`AttributeError` from resolving the factory path, and a `ValueError` or `TypeError` from
calling the factory (missing key, unknown option), into `ModuleLoadError("<factory>: <message>")`;
`python -m kayden` prints `kayden: <message>` to stderr and exits 1. Other exceptions keep
their traceback, since they are bugs rather than configuration mistakes.

`tests/test_e2e.py` is the end-to-end check. No API key was available, so it runs the real
Memory, Tools and runtime against the scripted Core: once through `run_instance` with the
config `python -m kayden` builds (Core and Frontend swapped for fakes), and once with the
real TUI driven by Textual's test pilot (send, approve the shell call, see the answer).
Both check the SQLite transcript and the file the shell call wrote. The live Core suites
run with `KAYDEN_LIVE_TESTS=1` and a key.
