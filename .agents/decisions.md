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
| `instance.lock` | `fcntl.flock` lock, taken at the first `start()`; contents: holder pid. `session.lock` and `continuous.lock`, left by the per-mode versions, are also locked when present, so an older Kayden cannot run on the folder meanwhile, and one that holds them makes `start()` fail naming its pid |
| `usage.json` | `{"schema_version": 1, input_tokens, output_tokens, cached_tokens, cost_usd, tool_calls, elapsed_s}`; written with temp file + `os.replace` at the first start, after each charge, each tool-call reservation and whenever activity drops to zero. When it does not exist, the first start sums `usage-session.json` and `usage-continuous.json` into it and leaves them in place |
| `events.jsonl` | one JSON object per line, written off the event loop by a `logging.handlers.QueueListener` thread. The per-mode `events-<mode>.jsonl` files are left as they are |
| `scratch/` | `ToolContext.data_dir`, shared by all calls |

Event log records:

```json
{"ts": 1793283281.2, "seq": 12, "event": "ToolStarted", "data": {...dataclass fields...}}
{"ts": 1793283281.2, "decision": "budget_check", "before": "tool", "ok": true, "reason": null}
```

Decision kinds: `start`, `cycle_requested`, `cycle_start`, `cycle_end`, `budget_check`,
`permission`, `backgrounded`, `cancel_operation`, `abandoned`, `late_completion`, `idle`,
`stop`, `stopped`, `resume_repair`, `reserved_tool_dropped`, `maintenance_retry`,
`activation_notice`, `duration_exhausted`, `set_autonomous`, `rename_conversation`
(`cleared`, never the title), `archive_conversation`, `delete_conversation`, `title` (an
automatic title was written; not its text), `memory_added`, `memory_edited`,
`memory_forgotten` (entry id only).
TextDelta, ReasoningDelta, ToolCallDelta and ToolProgress are not logged.

## Base system prompt

`config.DEFAULT_SYSTEM_PROMPT` explains backgrounded operations, `op_status`/`op_cancel`
and `recall` (with scope "all" for the other conversations). `[runtime] system_prompt` or
`system_prompt_file` replaces it. An autonomous conversation appends `AUTONOMY_PROMPT`
(how to idle, and that finished background work wakes it) to either.
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
- recall: one line per hit, `[conversation/message role ISO-time] text`, text capped at
  1000 characters. Scope "all" in a conversation with Remember off: ERROR "recall with
  scope "all" is not available here: the user turned off Remember for this conversation,
  so it stays private."
- Unsupported optional Memory methods: NotImplementedError "this memory module cannot edit
  memories" / "this memory module cannot delete conversations".

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
`Completion`, word-overlap archive search, one long-term list with revisions and a
forgotten set, seeding by copying the source's summary and tail without inference,
optional titles (`auto_title`) and optional-method support (`editable`), facts to extract
set through `extract`), `ScriptedFrontend`
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

## Module migration (.agents/process/MODULES.md)

Module streams were imported with `rsync -a --ignore-existing`, so this repository's
`api.py`, `__init__.py` files and runtime were kept. Dependencies were merged into
`pyproject.toml`: `anthropic`, `openai`, `httpx2` (the HTTP client both SDKs depend on),
`textual`; `psutil` for tests.

### Memory (`kayden.memory.sqlite`)

- Factory: `kayden.memory.sqlite:factory`. Options: `path` (default
  `<data_dir>/memory.db`) plus any
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
- Stores use only the stable tags from the start; no code reads Python import-path tags.
  Version 1 stores written by the pre-migration module with import-path tags are refused
  rather than read as opaque blocks. Schema versions: see "SqliteMemory schema 3" below.
- The archive is append-only: the source never runs UPDATE on `messages`, and DELETE
  only inside `delete_conversation` (see schema 3 below); the no-update/no-delete
  triggers stay in place for the life of a store.
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

Design: [Agent Note](notes/implemented/2026-10-30-deepseek-openai-compatible-profiles.md).

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
- Maintenance requests (`InferenceRequest.maintenance`, design: [Agent Note](notes/implemented/2026-11-02-maintenance-marker.md))
  run with thinking disabled, unless `options` says otherwise, so reasoning cannot use up a
  summary's token limit.
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

- Factory: `kayden.frontends.tui:factory`. Options `start`, `conversation_id`,
  `autonomous`, `model`: with `start` true the agent starts at once, otherwise the start
  screen asks. `run()` returns when the user quits or `events()` ends.
- History is loaded `HISTORY_PAGE` (100) messages at a time; a "Load earlier messages"
  button at the top of the transcript prepends the previous page (`history(before=...)`).
  Late-attach recovery follows `RuntimeSnapshot`'s order and also restores queued messages.
- Per-operation cancel: Ctrl+K lists `snapshot().operations` and cancels the chosen call;
  `/cancel <call id>` does the same directly.
- Permission choices are Allow once, Allow for conversation, Deny (`/allow once|conversation`,
  `/deny`). Runtime-side decisions (policy, Stop) are noted on the call via
  `PermissionResolved.call_id`.
- Activate (Ctrl+R, `/activate`) is offered once started; the runtime remains the
  authority and its errors are shown as notifications.
- Queued messages are matched to their commit by `message_id`. A BACKGROUNDED placeholder
  keeps the call shown as running in the background until its final ToolFinished or
  OperationResult. Refusals get a red "declined" line. The model shown follows
  `ModelSelected`.

## Running Kayden

Design of the config file: [Agent Note](notes/implemented/2026-11-02-instance-config-file.md).
User settings changed from a frontend (`update_settings`), saved to `settings.toml` and
applied over the config file: [Agent Note](notes/implemented/2026-11-07-settings-for-non-technical-users.md).

```
export ANTHROPIC_API_KEY=...
cd <project to work in>          # shell/file tools are rooted here
kayden [--config FILE] [--conversation ID] [--autonomous|--no-autonomous]
    [--model MODEL_ID] [--maintenance-model MODEL_ID]
    [--permissions ask|allow_read_only|deny] [--data-dir DIR]
```

The `kayden` command (`[project.scripts]`, installed with `uv tool install --editable`) and
`python -m kayden` both run `kayden.__main__:main`. `kayden/__main__.py` builds an `InstanceConfig`. The modules come from a TOML file:
`--config FILE` (must exist), else `<data dir>/config.toml` (optional). Sections `[core]`,
`[memory]`, `[tools]`, `[frontend]`, each with `factory` and `options`; anything else is
refused at startup. A module left out gets its default: `kayden.core.anthropic:factory`,
`kayden.memory.sqlite:factory` (`<data dir>/memory.db`), `kayden.tools.shell_files:factory`
rooted at the current directory (only when no tools options are given), and
`kayden.frontends.tui:factory`. The file is read with stdlib `tomllib`. `[runtime]` holds the
per-instance settings: `model`, `maintenance_model`, `permissions`, `system_prompt` or
`system_prompt_file`, and `[runtime.budget]` (the `Budget` fields, non-negative). Keeping the budget limit in the data directory, next to
the usage it limits, is the point: a flag alone would let each launch pick or forget the
limit. `--model`, `--maintenance-model` and `--permissions` override the file. The other
`RuntimeConfig` fields (retries, timeouts, ...) are tuning and stay at their defaults
until someone needs them. `--show-config` prints the effective config as TOML (`tomli-w`)
and exits. `--conversation` and `--autonomous`/`--no-autonomous` start at once: they
become the frontend options `start = true`, `conversation_id`, `autonomous`, merged over
the configured ones. `--mode` and `--activation` are accepted for one release: each prints
a warning naming `--autonomous` to stderr, starts at once, and `--mode continuous` or
`--activation agent_controlled` means `--autonomous` unless `--[no-]autonomous` is given.
The data directory defaults to `$KAYDEN_DATA_DIR` or `~/.kayden`. Without those flags the
TUI's start screen asks. The launcher checks no API keys: each Core refuses a
missing key at construction (the Anthropic Core accepts `ANTHROPIC_API_KEY` or
`ANTHROPIC_AUTH_TOKEN`).

Module startup errors are one line. `load_module` turns an `ImportError` or
`AttributeError` from resolving the factory path, a `ValueError` or `TypeError` from
calling the factory (missing key, unknown option), a malformed path and a factory that
returns the wrong kind into `ModuleLoadError("<factory>: <message>")`. For Core, Memory and
Tools that message becomes `Settings.startup_error` on the setup runtime (see Swappable
modules below); only when even the launcher's own frontend cannot be built does
`python -m kayden` print `kayden: <message>` to stderr and exit 1. Other exceptions keep
their traceback, since they are bugs rather than configuration mistakes.

`tests/test_e2e.py` is the end-to-end check. No API key was available, so it runs the real
Memory, Tools and runtime against the scripted Core: once through `run_instance` with the
config `python -m kayden` builds (Core and Frontend swapped for fakes), and once with the
real TUI driven by Textual's test pilot (send, approve the shell call, see the answer).
Both check the SQLite transcript and the file the shell call wrote. The live Core suites
run with `KAYDEN_LIVE_TESTS=1` and a key.

## Desktop app

Design: [Agent Note](notes/proposed/2026-11-07-desktop-app.md).

| Choice | Value |
|---|---|
| Entry points | `kayden-desktop` (`kayden.frontends.desktop.launcher:main`); bundles run `python -m kayden_desktop`, which calls it |
| Dependencies | `pyside6-essentials` and `qasync`, in the `desktop` extra (also in the dev group) |
| Default work folder | `~/Kayden`, created at launch when neither config file sets a root (`load_config(default_root=...)`) |
| At launch | Opens `--conversation` (given by the TUI's hand-over), else the most recently updated conversation, or starts one when there is none |
| Welcome | Shown on the setup runtime when the chosen provider needs a key and no provider has one; it saves the provider, key, work folder and limit, then restarts |
| Markdown re-render | At most every 100 ms while streaming; at once on commit |
| History page | 100 messages, then "Load earlier messages" |
| Quit order | `setQuitOnLastWindowClosed(False)`: closing the window ends `Frontend.run`, the runtime shuts down, then `asyncio.run` returns |
| "Get a key" pages | `platform.claude.com`, `platform.deepseek.com/api_keys`, `platform.openai.com/api-keys` |
| Shared with the TUI | `kayden.frontends.common`: permission labels, money formatting, `loosening()` |

**PySide6 on Python 3.15.** With PySide6 6.12 loaded, reading a lazily imported module
attribute (PEP 810) from another module returns the `lazy_import` object instead of
resolving it. `asyncio.to_thread` reads `concurrent.futures.ThreadPoolExecutor`, so every
`to_thread` failed. `kayden.frontends.desktop` resolves that name when imported, before any
Qt import; `tests/conftest.py` imports it first for the same reason. Other lazily imported
stdlib names stay exposed on 3.15.

**qasync results (2026-11-08, qasync 0.28.0, PySide6 6.12.0).** With
`KAYDEN_TEST_LOOP=qasync`, every test file passes on Python 3.13.18 when run on its own.
On 3.15.1, `test_commands.py` hangs and `core/test_openai_compat.py` segfaults in fixture
teardown; the other files pass. A single run of the whole suite segfaults on both versions
after a few dozen tests, each of which creates its own `QEventLoop`; the app creates one.
Bundles therefore carry Python 3.13.

**Briefcase (0.4.7).** It accepts `[tool.briefcase]` and generates the Linux system
template. The build stopped compiling its launcher stub: the machine's Python 3.14 had no
`Python.h`. No bundle has been built, so sizes are not measured yet.

**Permission ceiling.** The runtime reads the inherited ceiling once, at construction and
before exporting its own policy, and `update_settings` clamps to that. Clamping to the
environment at update time would clamp to the runtime's own export, and permissions could
never be loosened in a running instance (`test_permissions_loosen_past_the_policy_this_runtime_exported`).

## Changes for contract Draft 3 (autonomy into sessions)

Design: [Agent Note](notes/implemented/2026-11-07-autonomy-into-sessions.md).

| Choice | Value |
|---|---|
| `activate()` in IDLE | Allowed for every conversation; in an autonomous one it also drops a pending wake-up (a new cycle clears the wake set) |
| `start()` | Always leaves the agent IDLE. A resumed autonomous conversation has no wake set, so nothing runs before the user acts |
| `StartOptions` validation | `remember=False` or `seed_from` with a `conversation_id`: ValueError, before anything is opened |
| `set_autonomous` | Changes the in-memory conversation first (so a wake-up check during the write sees it), then Memory; reverts if Memory fails |
| Untitled display | `ConversationInfo.preview`: the Text of the first USER message, whitespace collapsed, 100 characters |
| `Memory.search` scope | `conversation_ids` (None = all). `recall(scope="all")` passes the open conversation plus every listed (not archived) conversation with Remember on; `search_archive` without an id searches everything, archived included |
| Memory entry ids | Strings; SqliteMemory uses its integer row ids |

### SqliteMemory schema 3

- Version 3 (`PRAGMA user_version`). Version 2 stores are migrated in place, in the
  transaction that opens the store; any other version is refused. A failed migration
  rolls back and leaves version 2 as it was.
- `conversations`: `autonomous`, `remember`, `archived`, `seed_from` columns. The
  migration sets `autonomous = 1` and the title "Continuous conversation" (if it had none)
  on the former continuous conversation, then drops the `mode` column rather than keeping
  it unused: nothing reads it, and new rows would otherwise need a made-up value.
- Long-term memory: `memory_entries` (id, source conversation or NULL for the user's,
  created_at) and `memory_revisions` (entry, text, created_at); the current text is the
  newest revision. The migration copies `long_term` (both modes) with exact duplicates
  collapsed into the lowest id, then drops it. `forgotten` holds SHA-256 fingerprints of
  forgotten texts (casefolded, whitespace collapsed); compaction drops a new entry whose
  fingerprint is there. A reworded fact is new to it.
- Remember off: `assemble` and `inspect` show no long-term entries and compaction adds
  none; the summary is written as usual.
- Seeding: while `seed_from` is set and the conversation has no summary row, `maintain`
  first runs the compaction prompt (light model) on the source's current summary and as
  many of its newest exchanges as fit, and stores the result as a summary with
  `through_seq` 0. Long-term lines in that output are ignored (the source already
  contributed them). An empty result or an empty source writes nothing; the next maintain
  tries again.
- Titles: after seeding and compaction, a conversation with no title gets one from its
  first USER message with Text (2000 characters at most), light model, 20 output tokens,
  prompt `_TITLE_PROMPT`. The first non-empty line is kept, quotes, `#*_` and a trailing
  period stripped, 80 characters at most. A CoreError is logged and leaves the title
  empty (the turn goes on, no Failure event); BudgetExhausted propagates. The write is
  `UPDATE ... WHERE title IS NULL`, so a title the user set meanwhile wins.
- Erasure: `PRAGMA secure_delete = ON` on every connection. `delete_conversation` runs in
  one transaction: list the id in `erasing` (the no-delete trigger exempts listed
  conversations), remove each message from the contentless FTS index with the `delete`
  command and its original text, delete messages, summaries, long-term entries from it
  and their revisions, set `seed_from` to NULL where it pointed there, delete the row,
  then `optimize` the FTS index so no segment keeps the terms. `forget` deletes the
  revisions and the entry and records the fingerprints. Both then run
  `PRAGMA wal_checkpoint(TRUNCATE)`; a concurrent reader can keep it from completing,
  which is logged as a warning.

## Swappable modules

Design: [Agent Note](notes/implemented/2026-11-08-swappable-modules-in-app.md).

| Choice | Value |
|---|---|
| Built-in ids | `anthropic`, `deepseek`, `openai`; `sqlite`; `shell-files`; `tui`, `desktop` |
| Display names | Claude, DeepSeek, OpenAI; Built-in memory (SQLite); Shell and files; Terminal (TUI), Desktop app |
| Discovery | `importlib.metadata.entry_points` per group; ids read from metadata alone at launch, factories imported only for `settings()`. A failing import is logged at INFO and the entry left out |
| Desktop entry point | `kayden.frontends.desktop` imports `qasync` and `PySide6.QtWidgets` at the top, so it loads only with the `desktop` extra |
| `kayden.core.openai_compat:factory` | Not registered: the profiles are `deepseek_factory` and `openai_factory`. A config naming the generic factory has no id ("Custom (from config.toml)") |
| Saved choices | `[modules]` in `settings.toml`, only the kinds changed. A legacy `provider` key is read as `modules.core` and dropped when the Core is next chosen |
| Options on a switch | A chosen module keeps `config.toml`'s options only when it is the module `config.toml` names; otherwise it gets none |
| Core change | Saves `model = ""` and `maintenance_model = ""` (the new Core's first, the main model), overriding `config.toml`. Models chosen while another Core waits apply live and are not saved |
| Restart | `RuntimeAPI.restart()` records a `Restart(frontend, frontend_kind, conversation_id)`, logs a `restart` decision and shuts down as on quit. `launch()` reloads the config and runs the next instance in the same process; the frontend gets `start = true` and the conversation unless the Memory changed |
| Terminal to window | After the instance has shut down (the lock is released), `kayden` runs `python -m kayden.frontends.desktop.launcher --data-dir D [--config C] [--conversation ID]` with `start_new_session`, standard streams to `/dev/null`, and prints that Kayden continues in the desktop app |
| Display check | macOS and Windows always; elsewhere `DISPLAY` or `WAYLAND_DISPLAY` set |
| Frontend per launcher | The saved frontend when installed and of the launcher's kind, else `tui` (`kayden`) or `desktop` (`kayden-desktop`). An uninstalled saved frontend is not a startup error: the launcher's own runs, and settings show the saved id as not installed |
| Startup errors | An uninstalled Core, Memory or Tools id, or a `ModuleLoadError` from building them, runs the frontend on `SetupRuntime`. A frontend that fails to build is replaced by the launcher's own, which shows the error |
| Setup runtime | Saves module settings, budget, permissions and system prompt; refuses model choices (`ValueError`) and every other command (`RuntimeStateError` naming the error). It takes no lock and writes no event log: nothing runs, and another instance may hold the directory |
| Work folder | The Tools `root` option, settable when the configured Tools has one (the built-in shell-files Tools always does) |
| Frontend texts | `kayden.frontends.common`: `restart_notice`, `module_options`, `needs_restart`, the Memory warning |
