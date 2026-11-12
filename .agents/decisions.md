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
| `permissions` | `auto` | clamped to any inherited ceiling |
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
| `instance.lock` | `fcntl.flock` lock, taken at the first `start()`; contents: holder pid. |
| `usage.json` | `{"schema_version": 1, input_tokens, output_tokens, cached_tokens, cost_usd, tool_calls, elapsed_s}`; written with temp file + `os.replace` in a thread, one write at a time with the latest totals, after the first start (awaited), each charge, each tool-call reservation and whenever activity drops to zero, and at shutdown (awaited). |
| `events.jsonl` | one JSON object per line, written off the event loop by a `logging.handlers.QueueListener` thread. |

Event log records:

```json
{"ts": 1793283281.2, "seq": 12, "event": "ToolStarted", "data": {"call_id": "c1", "name": "shell"}}
{"ts": 1793283281.2, "decision": "budget_check", "before": "tool", "ok": true, "reason": null}
```

`data` holds the event's dataclass fields, except that content is replaced: `MessageCommitted`
has `message_id`, `role`, `blocks` (type tags) and `stop`; `MessageQueued` has `chars`;
`ToolStarted`, `ToolFinished` and `PermissionRequested` name the call without arguments or
output; `SettingsChanged` has `system_prompt_chars`; `ConversationUpdated` has no title or
preview.

Decision kinds: `start`, `cycle_requested`, `cycle_start`, `cycle_end`, `budget_check`,
`permission`, `backgrounded`, `cancel_operation`, `abandoned`, `late_completion`, `idle`,
`stop`, `stopped`, `resume_repair`, `reserved_tool_dropped`, `maintenance_retry`,
`activation_notice`, `duration_exhausted`, `rename_conversation`
(`cleared`, never the title), `archive_conversation`, `delete_conversation`, `title` (an
automatic title was written; not its text), `memory_added`, `memory_edited`,
`memory_forgotten` (entry id only).
TextDelta, ReasoningDelta, ToolCallDelta and ToolProgress are not logged.

## Base system prompt

`config.DEFAULT_SYSTEM_PROMPT` explains backgrounded operations, `op_status`/`op_cancel`
and `recall` (with scope "all" for the other conversations). `[runtime] system_prompt` or
`system_prompt_file` replaces it. The system prompt is the same for a conversation's whole
life.
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
  1000 characters.
- Unsupported optional Memory methods: NotImplementedError "this memory module cannot edit
  memories" / "this memory module cannot delete conversations".

## Libraries

- `jsonschema` (added dependency) validates tool-call arguments against `ToolSpec.parameters`
  (`validator_for` the schema's draft; `best_match` error reported).
- `logging` QueueHandler/QueueListener for the non-blocking JSONL log.
- `ddgs` (keyless web search) and `trafilatura` (main text of a page as Markdown) for the
  web tools; pages are downloaded with `httpx2`, already a dependency of the Cores.
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
- `CompactionConfig` is all fractions of `ContextRequest.max_tokens`: trigger 0.9
  (summary plus active verbatim context; compaction runs before every request, so a big
  step is compacted before it is sent, and the 10% covers token-count estimates), remove 0.5 (cut-half), summary 0.05, long-term
  rendering 0.05. All token figures use the runtime's `TokenCounter`.
- Compaction cuts before a USER message that splits no call from its result (or pending
  call). When no such cut removes the share it should, as in one long task after a single
  message, it may also cut before an ASSISTANT message inside the turn. The message that
  turn started from then stays in view before the active messages (`_State.task`, read
  from the archive), so the agent keeps the task's wording and the request still opens
  with a user message. The request to the light model is sized so that system prompt, transcript
  and requested output fit `max_input_tokens(light=True)`: the cut moves earlier if needed,
  and a single oversized exchange is shown truncated (the archive keeps it whole). If not
  even that fits, maintain does nothing and returns False.
- The light model reads each tool call's arguments and each result cut to 2 000
  characters (`_SUMMARY_TOOL_CHARS`: the first and last 1 000, with a note that recall finds
  the rest). Command output and page text rarely belong in a summary; cutting them makes
  summaries cheaper and leaves room for the conversation. The archive keeps them whole.
- The summary ends with a topics list (`<topics>` in the reply, stored in the summary under
  "Topics so far (search the history for details):"), one line per topic with its date. The
  prompt says to carry every topic over and merge related ones as the list grows. Over the
  summary budget, the prose is cut first; the topics get up to half the budget, keeping
  whole lines from the top (the oldest, which the prose has lost). Kept in the one summary
  string, so the store's schema is unchanged.
- `assemble` priority: the newest exchange (from the last USER message on, truncated in
  view if it alone is too big), then the summary, then the newest long-term entries (at
  most 5% of the budget), then older verbatim messages as far back as fits.
- Storage format 2: blocks are `{"type": tag, "data": {...}}` with `BLOCK_TYPES` tags.
  `OpaqueBlock(type, data)` is stored as exactly that. An unknown tag, or a known tag whose
  data the current class cannot take (a field added later), decodes to `OpaqueBlock`, so it
  re-encodes unchanged. Appending a block class without a tag raises TypeError.
- Stores use only the stable tags from the start; no code reads Python import-path tags.
  Version 1 stores written by the pre-migration module with import-path tags are refused
  rather than read as opaque blocks. Schema versions: see "SqliteMemory schema" below.
- The archive is append-only: the source never runs UPDATE on `messages`, and DELETE
  only inside `delete_conversation` (see "SqliteMemory schema" below); the no-update/no-delete
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
- Anthropic thinking blocks are bound to what preceded them: the API answers 400 ("bound to
  a different conversation") when one is replayed after the system prompt, the tool set or
  an earlier message changed (editing the instructions, a
  compaction). The Anthropic Core stores a fingerprint of those (`Prefix`: SHA-256 of the
  system prompt, the sorted tool definitions and every earlier turn without its thinking)
  in each block's `state["prefix"]` and replays a block only while it matches; a block
  without one is not replayed. Should the API still reject a request for that reason, the
  Core sends it once more without any thinking and logs a warning. The API's own
  `prefix_mismatch_behavior: "drop_block"` (beta `thinking-binding-controls-2026-08-01`) was
  not used: it drops every block after the first mismatch on each request unless the
  failing blocks are removed from the history, which the Core cannot do, and it is only
  accepted with adaptive thinking.
- Stop reasons: Anthropic `refusal` and OpenAI `content_filter` map to REFUSAL.

### OpenAI-compatible profiles (`profile` option of `kayden.core.openai_compat:factory`)

Design: [Agent Note](notes/implemented/2026-10-30-deepseek-openai-compatible-profiles.md).

- A `Profile` is plain data in `kayden.core.openai_compat`; generic code reads its fields and never
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
  `model`: with `start` true the agent starts at once, otherwise the start
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
kayden [--config FILE] [--conversation ID]
    [--model MODEL_ID] [--maintenance-model MODEL_ID]
    [--permissions allow_workspace|allow_read_only|ask|deny] [--data-dir DIR]
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
and exits. `--conversation` starts at once: it becomes the frontend options
`start = true` and `conversation_id`, merged over the configured ones.
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
| App menu | `app_menu.install()` (Linux, from **Kayden → Add to App Menu**) writes `applications/io.github.k5031.kayden.desktop` and `icons/hicolor/scalable/apps/io.github.k5031.kayden.svg` under `$XDG_DATA_HOME` (default `~/.local/share`). `Exec` is this install's Python with `-m kayden.window`, since a menu has no terminal for `kayden`; the id is the window's desktop file name, so the window gets the icon. The action shows while the entry is missing or differs. Installing also runs `kbuildsycoca6` and sends KIconLoader's `iconChanged` signal when they exist: KDE keeps the entries and icons it has read, so without them a click runs the entry's old command |
| Entry points | No command of its own: `kayden` hands over to `python -m kayden.window`; bundles run `python -m kayden_desktop`, which calls the same `main`, or `kayden.__main__.main` when started as `kayden` or `kd` (links the package adds) |
| Dependencies | `pyside6-essentials` and `qasync`, in the `desktop` extra (also in the dev group) |
| Default work folder | `~/Kayden`, created at launch when neither config file sets a root (`load_config(default_root=...)`) |
| At launch | Opens `--conversation` (given by the TUI's hand-over), else the most recently updated conversation, or starts one when there is none |
| Welcome | Shown on the setup runtime when the chosen provider needs a key and no provider has one; it saves the provider, key, work folder and limit, then restarts |
| Markdown re-render | At most every 100 ms while streaming; at once on commit |
| History page | 100 messages, then "Load earlier messages" |
| Quit order | `setQuitOnLastWindowClosed(False)`: closing the window ends `Frontend.run`, the runtime shuts down, then `asyncio.run` returns |
| "Get a key" pages | `platform.claude.com`, `platform.deepseek.com/api_keys`, `platform.openai.com/api-keys` |
| Shared with the TUI | `kayden.frontends.common`: permission labels, money formatting, `loosening()` |
| Look | Fusion style plus a palette and style sheet from `theme.py`; light or dark from `QStyleHints.colorScheme()`, followed live |
| Accent | Indigo `#5B5BD6` (light), `#8C8CF5` (dark); the rest neutral gray |
| Text sizes | Relative to the system font: small 0.85×, title 1.15×, heading 1.45× |
| Sidebar row | Title (the title, else the first message, else "New chat"), date (time today, "Yesterday", weekday within a week, else "3 Nov", plus the year when not this year), and the first message as a second line when the title is not it |
| Your messages | At most 75 % of the transcript's width |
| Message box | Grows with its text to 6 lines |
| Event loop | `loop.QtEventLoop`, qasync's `QEventLoop` with three fixes (below); `KAYDEN_TEST_LOOP=qasync` runs the tests on it |
| Garbage collection | Automatic collection is off while a `QtEventLoop` is open; every 100 ms (`GC_CHECK_S`) the loop collects the oldest generation whose count passed its threshold |

**qasync fixes (`QtEventLoop`).** Python's cyclic garbage collector runs in whichever thread
allocates. A QObject it frees in an `asyncio.to_thread` worker (one importing the provider's
SDK during `update_settings`, say) cannot stop its timers from there ("Timers cannot be
stopped from another thread"); the next one to fire hits freed memory and segfaults. Every
closed qasync loop leaves such a QObject in a cycle, and the app's own may too, so the loop
collects on the Qt thread instead. qasync's `run_until_complete` also processes Qt events
once more after the loop stops; a task woken there fails with "not the running loop" and
never resumes, so the next `run_until_complete` waiting on it hangs (fixture teardown in the
tests, `asyncio.run`'s shutdown in the app). `QtEventLoop.run_until_complete` leaves those
callbacks for the next run, as asyncio does. On Python 3.15, transports pass `context` to the
loop's `_add_reader` and `_add_writer`, which qasync's do not take, so every socket read
failed; `QtEventLoop` takes it and runs the callback in that context.

**PySide6 on Python 3.15.** With PySide6 6.12 loaded, reading a lazily imported module
attribute (PEP 810) from another module returns the `lazy_import` object instead of
resolving it. `asyncio.to_thread` reads `concurrent.futures.ThreadPoolExecutor`, so every
`to_thread` failed. `kayden.frontends.desktop` resolves that name when imported, before any
Qt import; `tests/conftest.py` imports it first for the same reason. Other lazily imported
stdlib names stay exposed on 3.15.

**qasync results (2026-11-11, qasync 0.28.0, PySide6 6.12.0).** With
`KAYDEN_TEST_LOOP=qasync`, the whole suite passes on Python 3.15.1, repeatedly and in one
run, on `QtEventLoop`. Bundles carry Python 3.13.

**Briefcase (0.4.7).** The bundle installs Kayden as a package (`requires =
[".[desktop]"]`), not its sources: Kayden finds its modules through entry points, which
only package metadata carries (a bundle of sources opened the terminal frontend and listed
no modules). The Linux system package for Arch builds and starts the desktop app
(2026-11-10, system Python 3.14.9): 114 MB as `.pkg.tar.zst`, 398 MB installed, of which
PySide6 is 244 MB. Not built yet: the Flatpak (needs `flatpak-builder`) and the macOS
`.dmg` (needs a Mac).

**Permission ceiling.** The runtime reads the inherited ceiling once, at construction and
before exporting its own policy, and `update_settings` clamps to that. Clamping to the
environment at update time would clamp to the runtime's own export, and permissions could
never be loosened in a running instance (`test_permissions_loosen_past_the_policy_this_runtime_exported`).

## Free in the work folder

Design: [Agent Note](notes/implemented/2026-11-10-free-in-the-work-folder.md).

| Choice | Value |
|---|---|
| Default policy | `auto` ([automatic permissions](#automatic-permissions)); saved or configured levels are kept |
| `Tools.confined` | Called for every gated call before grants and policy; an exception is logged and counts as False |
| Grants | Tool name -> whether calls that reach outside are covered; a grant from a confined prompt covers confined calls only |
| File containment | `Path.resolve()` of the argument (default `.`) `is_relative_to` the resolved root, in a thread; a non-string path is not confined |
| Boundary | None when the root is the home directory or one of its parents (so also `/`) |
| Sandbox check | `bwrap ... true` once at construction with the starting root, 5 s limit; any failure leaves the shell unsandboxed. The command line is rebuilt for each folder (`use_folder`) |
| Home in the sandbox | An empty tmpfs directory at the same path, so `$HOME` exists; what a command writes there is gone when it ends |
| Shell start | `create_subprocess_exec(*sandbox, "sh", "-c", command)`, also without a sandbox |
| Step descriptions | [Agent Note](notes/implemented/2026-11-10-step-descriptions.md). `_DESCRIPTION_SCHEMA` in the orchestrator; frontends split a call with `frontends.common.call_parts` (one text argument as it is, several as `name: value` lines) |
| Sandbox environment | `os.environ` without names matching `KEY\|TOKEN\|SECRET\|PASSW\|CREDENTIAL` (case ignored); unsandboxed commands get it unchanged |

## Changes for contract Draft 3 (autonomy into sessions)

Design: [Agent Note](notes/implemented/2026-11-07-autonomy-into-sessions.md).

| Choice | Value |
|---|---|
| `activate()` in IDLE | Allowed; it also drops a pending wake-up (a new cycle clears the wake set) |
| `start()` | Always leaves the agent IDLE. A resumed conversation has no wake set, so nothing runs before the user acts |
| Wake-up | When a cycle ends, the wake set is every outstanding operation; the agent is woken once all of them have terminated, or at once when results it has not seen exist and nothing is outstanding. A user-cancelled operation wakes it like any other outcome; Stop does not |
| `StartOptions` validation | `seed_from` with a `conversation_id`: ValueError, before anything is opened |
| Untitled display | `ConversationInfo.preview`: the Text of the first USER message, whitespace collapsed, 100 characters |
| `Memory.search` scope | `conversation_ids` (None = all). `recall(scope="all")` passes the open conversation plus every listed (not archived) conversation; `search_archive` without an id searches everything, archived included |
| Memory entry ids | Strings; SqliteMemory uses its integer row ids |

### SqliteMemory schema

- Version 6 (`PRAGMA user_version`). A store of any other version is refused; Kayden
  carries no migration code. Erasing a row from `messages_fts` repeats the text it was
  indexed with, so `codec.searchable_text` must not change without a new schema version.
- `conversations`: `archived`, `seed_from`, `folder` columns.
- Long-term memory: `memory_entries` (id, source conversation or NULL for the user's,
  created_at) and `memory_revisions` (entry, text, created_at); the current text is the
  newest revision. `forgotten` holds SHA-256 fingerprints of
  forgotten texts (casefolded, whitespace collapsed); compaction drops a new entry whose
  fingerprint is there. A reworded fact is new to it.
- `summaries`: one row per conversation, its current summary; writing a new one deletes
  the old row in the same transaction (`_replace_summary`). Summaries are derived from
  messages the archive keeps, so they are working state, not part of the append-only
  archive.
- Seeding: while `seed_from` is set and the conversation has no summary row, `maintain`
  first runs the compaction prompt (light model) on the source's current summary and as
  many of its newest exchanges as fit, and stores the result as a summary with
  `through_seq` 0. Long-term lines in that output are ignored (the source already
  contributed them). An empty source writes nothing. An empty result writes nothing and
  skips that maintain's compaction too, since a first summary from compaction would end
  seeding; the next maintain tries again.
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
| Saved choices | `[modules]` in `settings.toml`, only the kinds changed. |
| Options on a switch | A chosen module keeps `config.toml`'s options only when it is the module `config.toml` names; otherwise it gets none |
| Core change | Saves `model = ""` and `maintenance_model = ""` (the new Core's first, the main model), overriding `config.toml`. Models chosen while another Core waits apply live and are not saved |
| Restart | `RuntimeAPI.restart()` records a `Restart(frontend, frontend_kind, conversation_id)`, logs a `restart` decision and shuts down as on quit. `launch()` reloads the config and runs the next instance in the same process; the frontend gets `start = true` and the conversation unless the Memory changed |
| Terminal to window | After the instance has shut down (the lock is released), `kayden` runs `python -m kayden.window --data-dir D [--config C] [--conversation ID]` with `start_new_session`, standard streams to `/dev/null`, and prints that Kayden continues in the desktop app |
| Display check | macOS and Windows always; elsewhere `DISPLAY` or `WAYLAND_DISPLAY` set |
| Frontend per launcher | The saved frontend when installed and of the launcher's kind, else `tui` (`kayden`) or `desktop` (the window launcher). A saved window frontend makes `kayden` hand over at launch ("Kayden opens in the desktop app.") when a display is available; without one, or when it is not installed, `kayden` prints why and runs the TUI. An uninstalled saved frontend is not a startup error: settings show the saved id as not installed |
| Startup errors | An uninstalled Core, Memory or Tools id, or a `ModuleLoadError` from building them, runs the frontend on `SetupRuntime`. A frontend that fails to build is replaced by the launcher's own, which shows the error |
| Setup runtime | Saves module settings, budget, permissions and system prompt; refuses model choices (`ValueError`) and every other command (`RuntimeStateError` naming the error). It takes no lock and writes no event log: nothing runs, and another instance may hold the directory |
| Work folder | The Tools `root` option, settable when the configured Tools has one (the built-in shell-files Tools always does) |
| Frontend texts | `kayden.frontends.common`: `restart_notice`, `module_options`, `needs_restart`, `MODULE_LABELS` (the modules settings offer: no Memory) |

## Web tools

Design: [Agent Note](notes/implemented/2026-11-10-web-search.md).

| Choice | Value |
|---|---|
| Search | `ddgs` `DDGS(timeout=10).text(query, max_results)`, in a thread; default 8 results, at most 20; "no results" is an empty list, other ddgs errors an error result |
| Search retries | ddgs picks engines at random and reports "No results found" when those refuse, so the same query can fail and then work: an empty or failed search is tried 3 times in all, 1 s apart; after that, "Nothing found … after 3 tries", saying the engines may be refusing searches for a while (checked 2026-11-10: two common queries each failed about one time in three) |
| HTTP 403 | Said to be a site that refuses programs reading it, with a hint to use another source. Kayden does not disguise itself as a browser to get past such blocks |
| Download | `httpx2`, 30 s per request, at most 5 MB, at most 5 redirects followed by hand |
| Public addresses | Every hop's host is resolved; any address that is not `is_global` is refused. Names that do not resolve are an error result |
| Extraction | HTML: `trafilatura.extract(output_format="markdown", include_links, include_tables, with_metadata)` in a thread; text types and JSON (pretty-printed) as they are; anything else is an error |
| Result size | 30 000 characters per call, continued with `start` |
| Untrusted header | Every result starts with a line saying the content is from the web and its instructions are not the user's |
| Tool timeouts | `web_search` 60 s, `web_fetch` 120 s |

## Folders per conversation

Design: [Agent Note](notes/implemented/2026-11-10-folder-per-conversation.md).

| Choice | Value |
|---|---|
| Stored folder | The resolved absolute path; None only for conversations created with Tools that have a fixed folder |
| New conversation | `StartOptions.folder`, else the `seed_from` conversation's folder, else `Settings.work_folder` |
| Order in `start()` | Validate, find the conversation (resume) or pick the folder (new), `Tools.use_folder`, then create; a failed create moves the Tools back to the open conversation's folder |
| Missing folder | `ShellFilesTools.use_folder` creates it (`mkdir -p`); OSError makes `start()` raise ValueError naming the folder |
| Replacing an empty chat | Frontends start the new chat, then delete the empty one (only when it has no messages loaded) |
| Event log | The folder path is not logged |

## Automatic permissions

Design: [Agent Note](notes/implemented/2026-11-10-auto-permissions.md).

| Choice | Value |
|---|---|
| Order in `_gate` | DENY, grants, read-only (ALLOW_READ_ONLY), confined (ALLOW_WORKSPACE and AUTO), review (AUTO), then prompt or non-interactive refusal |
| Review request | Maintenance model, `REVIEW_PROMPT` (runtime/review.py) as system prompt, one USER message, no tools, `maintenance=True`, at most 1 024 output tokens |
| What it sees | Text of USER messages among the newest 200 (activation notices left out), newest first up to 16 000 characters; the work folder; tool name, description and arguments (cut at 4 000 characters) |
| Answer | First line `ALLOW` or `ASK` (case and `*`, `` ` ``, `.` ignored), second line the reason (300 characters); anything else asks |
| Always ask | `review.always_ask` on the JSON of the arguments: the data folder (absolute, `~/`, `$HOME/` forms), then credential paths and tools (`CREDENTIALS`); logged as `permission_review` with `by: "rule"` |
| Failures | Exhausted budget (checked first), any exception from the request: ask, with a reason saying Kayden could not check. No transient retries |
| Stop | The review is raced against Stop with the grace period; the call is then not started |
| Log | `permission_review` with call id and `allow`; the permission record says `by: "review"`. The reason is not logged |

## Memory notes

Design: [Agent Note](notes/implemented/2026-11-10-memory-notes.md). Tools offered by the Memory module through `kayden.memory.tools` (with recall), not by the runtime.

| Choice | Value |
|---|---|
| Store | Schema 7 (8 adds duties): `notes` (note 1 is the profile, "About you", created with the store), `note_revisions` (title, description, text, writing conversation, time; the latest is current), `saved_exchanges` (per conversation, the last message the save pass read). `memory_entries`, `memory_revisions` and `forgotten` are gone |
| Titles | Unique with case ignored; saving by a title that exists writes that note and keeps its own title; leading `#` stripped from a title the save pass writes; the profile keeps its title and cannot be deleted |
| Prompt | Profile in full, then "title: description" of the other notes, most recently written first, within `long_term_fraction` (5 %) of the context budget |
| Tools | `read_note(title)`, `save_note(title, description, text)` (replaces the whole text; an empty description keeps the old one), `forget_note(title)`; a missing title answers ERROR listing the notes |
| Save pass | In `maintain` when the newest message is an answer calling no tool and comes after the last saved one; reads the messages since (the newest part when long), the profile, notes' texts within 5 % of the light model's window (most recently written first, stopping at the first that does not fit) and the rest as an index; writes `<save>` (whole text) and `<append>` changes; up to 1 024 output tokens; a CoreError is logged and the exchange is read again after the next answer |
| Runtime | `maintain` is called once more after a cycle's last answer, before IDLE; when it acted, `MemoriesChanged(saved)` names the notes whose latest revision is new (read with `notes()` before and after); the event log keeps their number, not their titles |
| Notice | Frontends show a non-empty `saved` as a line after the answer: "Memory updated: Trip, About you", with an "Open memory" link in the desktop app and "(F4 to see it)" in the TUI. Saves by the agent's tools show as steps, and the user's own edits are not announced |
| Compaction | Writes the summary only; facts are the save pass's |
| Checked | Against Claude Haiku 5.5 (2026-11-10): a self-introduction made a profile and a project note, small talk saved nothing, a later plan was appended to the project note; about one cent for three turns |

## Desktop zoom

| Choice | Value |
|---|---|
| Mechanism | The app font and every style-sheet font size scale by the zoom (`theme.set_zoom`), live; pixel sizes (padding, icons, radii) stay |
| Levels | 67, 80, 90, 100, 110, 125, 150, 175, 200 %, as in browsers |
| Keys | Zoom In: the platform's Zoom In (Ctrl++) and Ctrl+=; Zoom Out: Ctrl+−; Actual Size: Ctrl+0; all in a View menu |
| Stored | `<data dir>/desktop.json` (`{"zoom": 1.25}`), written 0.4 s after the last change; missing or unreadable means 100 % |
| Not a setting | A desktop-only view preference, so it stays out of `settings.toml` and the runtime |

## Step groups

Design: [Agent Note](notes/implemented/2026-11-10-step-descriptions.md).

| Choice | Value |
|---|---|
| Opened | Closed by default; the desktop app keeps a group open while its first step stays first |
| Live title | The latest step not yet ended, with its state (desktop: preparing, running, in the background; TUI: starting, running, in the background) |
| Ended title | One step: its sentence and its problem; several: "N steps" and problems counted by label ("1 failed, 1 declined") |
| Cancel | The desktop group line has Cancel for its live step; each step keeps its own |
| Step rows | No box; "done" is not shown, its duration is in the tooltip; other states stay visible |

## Waiting steps

| Choice | Value |
|---|---|
| Desktop | A committed call is `Phase.WAITING` ("waiting", no Cancel) until `ToolStarted`: it may wait for a permission prompt, the automatic review, or earlier calls |
| TUI | The group line shows "waiting" for a requested call, as the desktop app does |

## MCP servers

Design: [Agent Note](notes/implemented/2026-11-10-module-provided-tools.md).

| Choice | Value |
|---|---|
| Library | The official `mcp` SDK (2.3), `mcp.Client`, one task per server holding its connection |
| Tool names | `mcp__<server>__<tool>`, characters outside `[A-Za-z0-9_-]` replaced by `_`, cut at 64 |
| read_only | The server's `read_only_hint`; absent counts as not read-only |
| Confined | Never: AUTO reviews each call, stricter levels ask |
| Environment | The SDK's safe default (HOME, LOGNAME, PATH, SHELL, USER), the server's own variables, and `PERMISSION_CEILING_ENV` |
| Start | At the first `list_tools` (the first request), all servers at once, 30 s to start and list; one that fails is left out and logged |
| Settings | `[[mcp_servers]]` in settings.toml: name, command or url, env as variable names; values in the keychain as `mcp/<server>/<NAME>`; removed variables are erased from it |
| Apply | Like modules: at the next start (Restart now), as one more Tools module after the configured one |
| Names | 1 to 32 of letters, digits, `-` and `_`, unique |
| Results | Text content joined; other content shown as `[<type> content]`; structured content as JSON when there is no text |

## The provider's web search

Design: [Agent Note](notes/implemented/2026-11-10-provider-web-search.md).

| Choice | Value |
|---|---|
| Tool | `web_search_20260209` for models with dynamic filtering (Opus 5.5, 5, 4.8–4.6, Fable 5, Sonnet 5.5, 5, 4.6, by id prefix), else `web_search_20250305`; `max_uses` 5 per request |
| Cost | $0.01 per search (`usage.server_tool_use.web_search_requests`) as `Usage.extra_cost_usd`; the results are read as input tokens on top |
| Steps | One `ServerToolUse` per web search or web fetch, placed where its result came; the code dynamic filtering runs is not shown (unless a turn has nothing else) |
| Replay | A turn's server blocks, all of them in the order they came (they refer to each other), ride on its first shown use and are sent back unchanged, to the same model, while web search is on, and only after the user's latest message; earlier turns' searches go as text (summary and found pages). Checked against Opus 5.5 (2026-11-10): a second turn replaying the whole set cost about three times the first |
| Fallbacks | A 400 saying web search is not enabled switches it off for the instance (Kayden's own search is offered again); a 400 while replaying server blocks resends them as text |
| Paused turns | `pause_turn` is continued in the same request, up to 5 times, the turn's blocks sent back as the assistant's |
| Sources | The cited pages, deduplicated in citing order, as a final "Sources:" line of links |

## One window per data folder

| Choice | Value |
|---|---|
| Asking | A desktop app that finds the data folder in use (`Settings.already_open`) connects to the open app's Unix socket and sends `show`; the open app brings its window forward (show normal, raise, activate) and the second exits without a window. Asyncio sockets, so it works on any loop |
| Socket | `$XDG_RUNTIME_DIR` (else the temp folder) `/kayden-<first 16 hex of SHA-256 of the data folder's path>.sock`, short enough for a socket name; a stale one is replaced when the next app listens, and it is removed on close |
| No answer | The terminal app has the folder (or a crash left it locked): one message, "Kayden is already open, probably in a terminal…", then the app closes |
| Terminal app | With `already_open` it exits at once (return code 1) with the error and what to do, instead of opening Settings |
| Raising | Desktops may refuse to give a background program's window focus (KDE's focus-stealing prevention); the window then asks for attention in the task bar instead |

## Duties

Design: [Agent Note](notes/implemented/2026-11-11-duties.md).

| Choice | Value |
|---|---|
| Intervals | Default minimum 1 minute, maximum 1 hour (`Duty.min_interval_s`, `max_interval_s`); the frontends take minutes |
| Spending limit | `Budget.max_cost_usd` must be set: `set_duty` refuses without it; a duty whose limit was removed later pauses when the cycle ends, with `Failure(BUDGET)` |
| Prompt | Appended to the system prompt after Memory's addendum: "## Your duty" (instruction, how it is woken, finish_goal for a goal) and "## Plan" (one line per item: status, text, due in local ISO time, note). A plan without a duty is shown alone. The current time is not in the system prompt, which would change every request and void the provider's prompt cache; every user message carries its sending time (Message times) and update_plan's result gives the time |
| Notice | A cycle the runtime starts for a duty begins with a USER notice: "New goal: <instruction>" (or "New duty: …") when it is turned on, so the task stays in the transcript after a goal ends mid-cycle, when the system prompt no longer has it; then "A step in your plan is due.", "Time to check in on your duty." or "Background work finished.", each followed by "Carry on with your duty, or end your turn if there is nothing to do now." The event log keeps the cause ("duty: <cause>"); causes: the duty was turned on, a plan item is due, scheduled check-in, background work finished. User-message cycles get none |
| update_plan | Replaces the whole list; `due` is ISO 8601 (local time without an offset) or `due_in_minutes`; an unreadable time is an ERROR result and changes nothing. Offered while the conversation has a duty or a plan; finish_goal only with a goal |
| Schedule | Computed when a cycle ends idle (an asyncio timer); cancelled by any cycle request, Stop, start, failure, ending the duty and shutdown; recomputed when the duty or plan changes while idle. Overdue items wake at the minimum interval |
| Events | `ConversationUpdated` for duty and plan changes; `DutyScheduled(wake_at)` and `RuntimeSnapshot.duty_wake_at` for the next wake; the event log records `duty`, `plan` (item count), `duty_scheduled` (delay, cause), `duty_paused`, `goal_finished` and each `cycle_requested` reason ("duty: <cause>") |
| Asking for help | `ask_for_help(question)`, offered with a duty: the question is held in memory (`HelpNeeded`, `RuntimeSnapshot.help_needed`), not stored; while it is open the schedule is off (`duty_paused` "waiting for help") and finished operations still wake the agent; `send`, `activate`, `set_duty` and `start` clear it. Frontends show it as a warning line and "Duty waiting for your answer" in the status; the event log keeps its length only |
| Store | SQLite schema 8: `conversations.duty` (JSON or NULL) and `conversations.plan` (JSON list, default `[]`) |
| Plan text | The TUI and desktop edit the plan as lines: `[status] step \| due YYYY-MM-DD HH:MM \| note` (`frontends.common.plan_text`, `parse_plan_text`) |
| Commands | TUI: `/duty <instruction>`, `/goal <instruction>`, `/duty off`, `/duty` or `/plan` or F5 for the screen. Desktop: **Kayden → Duty…** |

## Message times

| Choice | Value |
|---|---|
| Stamp | In the request only, the first text block of every USER message (the user's, runtime notices, the activation notice) starts with `[Sent Wed 2026-11-11 10:15 +01:00] `: its `created_at` in local time with weekday and offset. Tool results and operation results are not stamped |
| Why there | Computed from the stored time, so a message reads the same in every request and the provider's prompt cache holds; the transcript, the frontends and search keep the text as typed |

## Headless frontend

Design: [Agent Note](notes/implemented/2026-11-11-headless-units.md).

| Choice | Value |
|---|---|
| Launch | `kayden --headless` replaces the frontend for that run with `kayden.frontends.headless:factory`, whatever is saved, and never hands over to the window; it is not an installed frontend, so Settings never offers it. `--duty` / `--goal` (one of them, with `--headless`, not with `--conversation`) become the frontend options `start`, `duty`, `goal` |
| Channel | JSON-RPC 2.0, one message per line, lines up to 16 MiB. At start the frontend keeps duplicates of fds 0 and 1 for the channel, points fd 0 at /dev/null and fd 1 at stderr, and sets `sys.stdout` to stderr |
| Requests | Every RuntimeAPI command but `events` and `restart`, read from the protocol itself (`kayden.frontends.wire.COMMANDS`); parameters are decoded by the command's type hints (`command_args`), `start` also takes its options as the request's params. Each handled in its own task, so a slow one blocks nothing; a request without an id gets no reply. Errors: -32700 bad JSON, -32600 not a request, -32601 unknown method, -32602 missing or unexpected parameters, -32000 refused by the runtime (`data.type` the exception's class) |
| Encoding | Dataclasses as objects with `"type": <class name>` and their fields; enums as values; tuples as lists. Requests take plain JSON (a duty and plan items as objects without `type`) |
| Events | Every envelope, deltas included, as an `event` notification with `seq` |
| Start-up | With `start` the frontend starts (a new conversation with a duty); a failure is written to stderr as `kayden: <message>` and the unit quits |
| Prompts | `interactive_permissions=True`: prompts wait for `resolve_permission` like a person's |
| Quit | stdin closing: requests already read are answered, then `run()` returns |

## Sub-sessions

Design: [Agent Note](notes/implemented/2026-11-11-sub-sessions.md).

| Choice | Value |
|---|---|
| Tools | `start_session(task, budget_usd=1.0)`, `message_session(session, text)` (both with no timeout: the child's spending limit bounds them), `stop_session(session)`. Not confined: reviewed under AUTO, asked under stricter policies |
| Child | `<sys.executable> -m kayden --headless --data-dir <data>/sessions/<first 8 of conversation id>-<n>`, cwd the work folder; config.toml copied, settings.toml copied with `budget = {max_cost_usd = budget_usd}` and `work_folder`; then `start` and `set_duty` (goal, check-ins every 5 s to 2 minutes). Reopening: the same with `--conversation <its conversation>` |
| Ids | "1", "2", … per data folder, continuing after the highest recorded |
| Record | `session.json` in the child's folder: id, task, parent conversation, state, report, question, failure, spent, child conversation; written off the loop when it changes. At start a record whose state was running becomes "stopped" |
| States | starting, working (also between a goal's check-ins), waiting (its question, after `HelpNeeded` and idle), idle (reopened with nothing to do), done (goal ended and idle; the report is `finish_goal`'s), failed, stopped; "Needs your approval" while a prompt is open |
| Prompts | Wait for the user, who answers them where the child is open; the parent's call keeps waiting |
| Process | Closed (stdin, then killed after 10 s) when it is done or stopped and no view has it open; cancelling the call, `stop_session`, Stop and closing the manager stop it |
| Proxy | `kayden.frontends.remote.RemoteRuntime` over `Channel` (JSON-RPC client): every RuntimeAPI command but `restart`, each method made from the protocol (same signature, a request of that name, the reply decoded by its return type); `snapshot()` is read once and kept current from the events (state, conversation, model, usage, duty wake, help, operations, prompts, queue). A reply to a cancelled request is ignored. `kayden.frontends.wire` encodes and decodes the values |
| Desktop | The sidebar groups conversations by folder (a heading with the folder's name, folded by a click; "Other" for none) and lists each conversation's children under it (indented, a tree line, a dot while working, the summary as second line). Clicking a child shows it in the conversation area through its own Controller and model; clicking a conversation returns. Memory, tool servers and settings stay the window's own |
| Initiation | The agent starts a child when the user asked for the work, and proposes `start_session` (Proposals) when it is its own idea |
| Notices | When a child's agent goes from working to not working, its state is not "working" (a goal between check-ins is), and no parent call waits on it: `[Runtime notice] Sub-session <id> (<name>) ended a turn: <state>.` with its question, report, or last reply (first 400 characters). Kept per parent conversation; the frontend sends them with `send` to its own runtime when that conversation is open (on each change, and after opening a conversation) |
| Resume | No automatic resume after a restart (SPEC 11). `resume`: start it again on its conversation, then `activate`; offered when it is stopped, failed or idle with its goal not reached. `message_session` starts a stopped child again too |
| Limits | The user's limit per sub-session (default $1) is kept by the manager in `<data>/sessions/limit.json`, not in the runtime's settings; `start_session` gives `min(budget_usd or limit, limit)`, and its description states the limit. Desktop: a Settings field; TUI: a `/sessions` button. A child's own limit changes with `set_child_limit`: through the child's `update_settings` while it runs, else in its settings.toml; raising either is confirmed. Rows show "$spent of $limit" |
| Deletion | Deleting a conversation (desktop and TUI) deletes its sub-sessions after it (`SubSessions.delete_of`), archived ones included; the confirmation names them. Archiving the parent hides them, since they are listed only under it |
| Row actions | Rename (`title`, empty shows the task), Archive (hidden unless Show archived), Delete (stop, then the folder is erased after confirmation). Recorded in `session.json` with `has_goal` and `budget_usd` |
| TUI | Opening a child shows it in place: the app's runtime is swapped for the child's `RemoteRuntime` (a new event pump, then a resync); `/back`, `/sessions`, `/conversations`, settings, memory, `/mcp` and `/folder` return to the app's own first. The status line starts with "Sub-session: <name> (/back returns)". `/sessions`: the list with Open, Start, Dismiss, Resume, Stop, Rename, Archive, Delete and Show archived |

## Proposals

Design: [Agent Note](notes/implemented/2026-11-11-proposals.md).

| Choice | Value |
|---|---|
| Tool | `propose(call: {name, arguments}, reason)`, a runtime built-in (`BUILTIN_TOOLS`), offered in every request. Only a Tools module's or the frontend's tool can be proposed; another name is an ERROR result saying it is not a tool, or needs no approval. The proposed call is validated like a call |
| Operation | The proposal is an operation with the `propose` call's id, backgrounded at once (no initial wait), spec timeout none while it waits. The proposed call gets the id `<propose id>-call`. On approval its tool's own timeout (else the default) applies |
| Approval | The click is the permission: no gate, logged as a `permission` decision `by="proposal"`. A tool call is reserved from the budget only on approval. Waiting is not activity for the duration budget |
| Wake-up | Pending proposals are left out of the wake set; a finished proposal operation sets a flag that wakes an idle agent, cleared when the next request is built |
| Withdrawn | `op_cancel` on its id, Stop, and `start()` (switching conversations) cancel pending proposals: `ProposalResolved(id, None)`. Pending proposals are not in `RuntimeSnapshot.operations` nor counted in `StateChanged.pending_ops` |
| Results | Approved: the call's own output. Dismissed: "The user dismissed the proposal; it did not run." (OK status) |
| Cards | Desktop: `ProposalCard` with the permission cards ("Kayden proposes: <step sentence>", the reason, other arguments; a `start_session` shows its task), Approve and Dismiss. TUI: `ProposalPrompt`, `/approve` and `/dismiss` answer the first open one |

## Runtime notices

| Choice | Value |
|---|---|
| Marker | `api.NOTICE_PREFIX` ("[Runtime notice] ") starts every USER message Kayden writes for the model: the activation notice, a duty's wake-up, a sub-session's report to its parent |
| Shown | Frontends show such a message as a notice line without the prefix (desktop `NoticeItem`, TUI `SystemEntry`), never as the user's bubble; a long one (several lines or over 160 characters, such as a new goal's brief) is folded to its first line, with Show all on the desktop and a collapsed entry in the TUI; a queued one is shown once it is committed. A conversation whose first message is a notice is labelled "New chat" until it gets a title |

## Desktop look (a calmer, more professional window)

| Choice | Value |
|---|---|
| Column | The conversation, the prompt cards and the message box sit in a centred column at most 780 px wide (`COLUMN_WIDTH`) |
| Bubbles | Your messages are a neutral bubble (`Palette.bubble`) with ordinary text, not the accent colour; a queued one is outlined |
| Sidebar | A ☰ menu button and a plain "+ New chat" row on top; conversation and sub-session rows are one line in normal weight (preview, date and state on hover); the open one is a subtle grey (`Palette.select`) |
| Menus | No menu bar. ☰ holds the conversation's actions (New chat, Duty, Stop, View with zoom and Show archived conversations, Model); the profile chip at the bottom left (a letter avatar and the OS user name, `getpass.getuser()`, with spending in its header) holds the app's (Settings, Memory, Tool servers, Add to App Menu, Help → the docs, Quit), and the gear beside it opens Settings. Every action is also the window's, so shortcuts work with the menu closed; Settings is Ctrl+, explicitly (Qt's Preferences key is unbound on Linux) |
| Replies | List items have no top margin, only `LIST_ITEM_SPACING` below |
| Header | The conversation's title only; its folder is the title's tooltip. No spending in view: it heads the profile menu, Settings shows it, and one warning notice appears when spending passes 80% of the limit (`LIMIT_WARNING`). Choosing another folder is ☰ → New chat in another folder… |
| Title bar | None from the desktop (`FramelessWindowHint`): the header and the sidebar's top row are `DragArea`s (drag moves the window through the window system, `startSystemMove`; double-click maximizes or restores), the header ends with minimize, maximize/restore and close, which fill its height and reach its top and right edges so a cursor pushed into the corner of a maximized window lands on close, and `WindowEdges` resizes from a 6 px border (`startSystemResize`) with the matching cursor. The desktop's own title-bar menu is gone |
| Message box | A filled box (`Palette.composer`), two lines high at least, the send or stop button plain at its bottom right; focus only lightens the border. Under it: the status only when it says something other than "Ready", and the model's name (from `list_models`), which opens the Model menu |
| Permission picker | Under the message box, before the model: the policy in a word (`PERMISSION_SHORT`), opening the five choices; a looser one is confirmed, a stricter one applies at once (`update_settings`). It follows the window's own runtime, also while a sub-session is shown |
| Settings window | Frameless, its own title and ✕ (dragged by its title row); sections down the left (General, Spending, Permissions, Instructions, Memory, Tool servers) and rows with a name and a one-line explanation on the left and the control on the right. Memory and Tool servers are the old dialogs embedded as pages; the profile menu's Memory and Tool servers open Settings at them. Save and Cancel stay |
| Status | No status line under the message box: Stop is the send button, working sub-sessions are dots in the sidebar, notices say the rest. A duty's next check-in (or that it is paused) is a small note beside the title |
| Start | The window opens maximized |
| Reply footer | Under each finished reply, shown while the pointer is on it and keeping its space when hidden: a copy button (the reply's Markdown; a tick for 1.5 s) and its age (`relative_age`: just now, N minutes or hours ago, then the date) |
| Sidebar tooltips | Only the full title: a conversation's title or whole first message, a sub-session's title or its brief's first line |
| Project headings | The name, then a drawn chevron: down when open, right when folded |
| Turn gap | Your message has 6 px more above it and 18 px more below it than the 12 px between items, so a new turn reads as one and Kayden's reply starts clearly apart from it (about 38 px, as in Claude) |
| Reply text | Lines at 118% of the font's line spacing (about 1.6 em; 145% made them nearly double-spaced); inline code at 0.88 of the text size on the selection colour, code blocks at that size without it |
| Sidebar look | Rows and New chat at 0.92 of the system font (the header title at 1.0, semi-bold), so the list reads lighter at any system size; the menu and New chat use drawn line icons like the window controls; sub-sessions hang on one thin guide line instead of a corner each; a working sub-session's dot has its own room, clear of the title |
| Mascot | A new chat shows the owner's mascot peeking (`desktop/assets/mascot-peek.png`, 200 px wide, scaled for the screen's density), in place of the K avatar. Cut from the owner's original sheet on white: the pose cropped on its own (rembg's anime model loses the smaller poses on a whole sheet), stray bits of neighbouring poses painted white, then `rembg -m isnet-anime`; only the mascot's shape kept and the image trimmed. The speed lines do not survive the cut |
| Folder picker | In the middle of a new chat, under its greeting, as an outlined button with the folder icon; only before its first message (and only when the Tools take a folder): the folder's name; its menu lists that folder (checked) and those of recent conversations, newest first, six in all, then Choose folder…. Picking one starts the new chat there |
| Sidebar | Starts 320 px wide at 100% zoom, scaled with it; the divider can be dragged, and the width is not saved |

## Compaction lines

| Choice | Decision |
|---|---|
| Signal | Memory marks its summarizing Completion call with `compacting=True`; the runtime emits `CompactionStarted` on it and `CompactionFinished` when maintain returns. `MaintenanceStarted` alone could not be used: upkeep runs after every answer. |
| "Compacted" | maintain succeeded and returned True, and the compacting call's output was not empty. A summary Memory then rejects while saving something else counts as compacted; that needs a reply that parses to nothing, and it only mislabels one line. |
| Look | A notice line in the transcript, "Compacting conversation…", changed to "Conversation compacted", or removed when nothing was compacted. Desktop and terminal app alike. Lines are not kept across restarts. |

## Wrap-up card

| Choice | Decision |
|---|---|
| Limits | `question` (required) up to 100 characters, one to three options (required) with labels up to 40, optional `recap` up to 400; at most one option carries a call |
| Withdrawal | A user message cancels a pending card proposal before it is appended; the cancelled outcome goes in first and no wake-up of its own follows |
| Close | ✕ clears the card in that window's model only; nothing is saved, so reopening shows it again |
| Stale action | After a restart the card proposal is gone; its option then sends its label as a message |
| Clearing | Only a message the user wrote (sent or queued) clears the card; background results and runtime notices leave it up. A closed card stays closed when the history is reloaded in that window |
| Options | Told apart by position: labels need not be unique |

## New chats

| Choice | Decision |
|---|---|
| Models at start | `start` asks the Core for its models only the first time, or when the chosen model is not in the list it has: a provider round trip on every switch was felt as lag |
| Switching in the desktop app | `Controller` returns from opening a conversation once it is loaded into the model, so the sidebar refreshed after it highlights the new one |
| Choosing a row | `ConversationList.chosen`: a click (itemClicked) or Enter. itemActivated is not used: where a single click activates (KDE), it comes with the click |
| Last click wins | Each request to show a conversation or sub-session is numbered (`Opening`); the sidebar highlights the newest while it opens, and only it is shown when done. A sub-session that finishes opening after a newer request is released. The controller runs conversation switches one after another, so the last asked for stays open |
| Draft | `start` without a conversation id or `seed_from` opens a draft: the runtime names it (`_new_id`) and keeps its info, folder included, without calling Memory. `history`, `search_archive` and `inspect_memory` answer for it as for an empty conversation |
| Stored | On first use: the first append (a message, an activation notice), a duty or plan, a rename or an archive. The runtime calls `Memory.create_conversation(conversation_id=…)` with the draft's id and emits `ConversationUpdated`, which refreshes lists |
| Continue from | Stored at once: Memory needs the source to seed the summary, and the chat is not blank anyway |
| Leaving | A draft left for another chat is dropped with nothing to delete; so is one open when the instance stops |

## read_around

| Choice | Decision |
|---|---|
| Contract | `Memory.history(after=…)` reads on from a message: with `limit`, the first `limit` after it. With `before` too, the messages between |
| Tool | `read_around(message_id, conversation_id?, before=3, after=3)`, up to 20 a side: the message marked `→`, in order with its neighbours, each line as a recall hit's (1000 characters at most). Found by reading on from the message and back from the next one, so no lookup by id is needed beyond `history` |
| Offered | With `recall`, also by a Memory that keeps no notes |
| Next | Segment summaries ([proposed note](notes/proposed/2026-11-11-segment-summaries.md)) build on it |

## The plan as a checklist

| Choice | Decision |
|---|---|
| Offered | `update_plan` with every open conversation, not only with a duty or a plan; its description says when to keep one (several steps) and when not (a quick answer) |
| Shown | While a step is todo or doing (`common.checklist`): desktop `PlanCard` in the conversation under the newest message, scrolling with it, a rounded card as in Claude's app: a filled tick for done (dimmed), a turning arc for the step under way (`Spinner`), a dashed circle for the rest, dropped steps struck through; terminal `PlanPanel` at the top of the prompts. Gone once every step is done or dropped |
| Steps | `update_plan` and `wrap_up` calls are not shown as steps (`common.CARD_TOOLS`): the checklist and the card show them |
| Caching | The plan is in the system prompt, as with duties and the Memory summary, so a plan change starts a new prompt prefix |

## Streaming thinking (Anthropic)

| Choice | Decision |
|---|---|
| Default | For models that always think (`claude-opus-5*`, `claude-sonnet-5*`, `claude-haiku-5*`, Fable, Mythos), a request without `thinking` in the merged options gets `{"type": "adaptive", "display": "summarized"}` (`anthropic._thinking_shown`). The API's default, `omitted`, streams empty thinking blocks, and on Opus 5.5 the notes between tool calls come back as thinking too: a long silence, then the whole answer. `display` changes what is shown, not what is billed |
| Configured | Any `thinking` in the Core's or the model's options is sent as it is |
| Not `updates` | `display: "updates"` (a beta) would show only the notes between tool calls; `summarized` also shows the reasoning, which the Thinking section already holds |
| Desktop | The Thinking button reads "Thinking…" while the reply has no text and is not final |

## What the saving pass keeps

| Choice | Decision |
|---|---|
| Keep | What stays true and the agent cannot find again: the user, how they want answers, their decisions and why, lasting facts about their work or setup |
| Skip | News (recent work, the state of a task), what files, code, docs or git history already say, small talk, one-off requests, what is saved |
| Upkeep | A note shown in full that holds news or something no longer true is saved again without it, so memory stays current without the user |
| Limit | Notes listed by title only are not reviewed; cleaning them needs them shown, which costs the light model's input |

## Two Memory modules

| Choice | Decision |
|---|---|
| Shared archive | `SqliteArchive` (`kayden.memory.sqlite`): messages, summaries, search, conversation metadata, erasure. Hooks `_long_term(info) -> LongTerm(head, lines, render)`, `_after_answer`, `_erase`. `SqliteMemory` adds notes (schema 8, `memory.db`); `FilesMemory` adds the memory folders (schema 1, `memory-files.db`) |
| Budget | The head (rules or profile) whenever it fits the room, then lines from the top within the long-term fraction |
| Files as notes | Id and title = path, description = frontmatter `description`, text = body. Saving writes Claude Code frontmatter (not for `MEMORY.md`); a new title renames the file. No revisions |
| Folder | The runtime passes the open conversation's work folder to `notes`, `save_note`, `delete_note`; `SqliteMemory` ignores it |
| Fixed | `MemoryNote.fixed` (the built-in profile) replaces the frontends' title check |
| Tool | `memory`, Anthropic's memory tool commands, run in a thread; read-only `recall` and `read_around` come from the archive |

## Skills

| Choice | Decision |
|---|---|
| Name | `[a-z0-9][a-z0-9-]{0,63}`; frontmatter `name`, else the folder's name |
| Description | Frontmatter `description`, else the instructions' first paragraph; one line, 500 characters at most in the listing |
| Order | Listed by name; Kayden's, then `<data dir>/skills`, then `<work folder>/.claude/skills`, the later replacing the earlier |
| Reread | With every request and every `RuntimeAPI.skills()` call, in a thread |
| Tool | `skill`, read-only, never gated or counted; a leading `/` in the name is dropped; an unknown name answers ERROR with the names |
| Prompt | The section follows the user's instructions and precedes Memory's addendum and the duty, so the stable part of the prompt comes first |

## Prompt caching (Anthropic)

| Choice | Decision |
|---|---|
| Default | Every turn request carries top-level `cache_control: {"type": "ephemeral"}` (`anthropic._cached`): the API marks the end of the request, so the next one reads the conversation so far from the cache. Measured on Opus 5.5: a second turn's 11.5k tokens of prompt cost $0.003 instead of $0.046 |
| Not cached | The runtime's one-off requests (`InferenceRequest.maintenance`: titles, summaries, the save pass, permission reviews): nothing would read what they wrote, so they would only pay the write premium |
| Configured | A `cache_control` in the Core's or the model's options is sent as it is (`ttl = "1h"`) |
| Cost | Written tokens count as input at the input price; the premium (25 %, or 100 % for an hour's cache) is added as `Usage.extra_cost_usd`, from the model's input price |
| Limit | The plan, the duty and Memory's addendum are in the system prompt, which comes before the messages: when one changes, the next request writes the conversation again instead of reading it |
