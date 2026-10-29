# Import and migrate the module implementations

Three module implementations exist outside this repo, written against older drafts of the contract:
`~/Projects/kayden-memory`, `~/Projects/kayden-core`, `~/Projects/kayden-tools` (Memory on SQLite; Anthropic and OpenAI-compatible Cores; shell/file Tools and a Textual TUI). Treat those folders as read-only.

## Steps
1. Copy `src/` and `tests/` from each into this repo, never overwriting existing files (`rsync -a --ignore-existing --exclude __pycache__`). Merge their dependencies into `pyproject.toml`. Make sure `.gitignore` excludes `__pycache__`, `.pytest_cache`, `.venv`, `.ruff_cache`. Commit as "Import module streams (pre-migration)".
2. Run `uv run pytest`. Expect failures where the old code uses older contract shapes.
3. Migrate ONE module at a time, in this order: Memory, Core, Tools, then the TUI. For each: fix until its conformance suite and tests pass, run `ruff`, commit.
4. Run the whole suite together at the end, then do one end-to-end check described below.
5. Record contract problems in `docs/contract-issues.md` (new section) and choices in `docs/decisions.md`. Do not edit `SPEC.md` or `api.py`.

## What changed in the contract since those modules were written
### Memory
- Add `history(conversation_id, *, before=None, limit=None)` reading the full archive.
- `maintain(conversation_id, complete, context) -> bool`; thresholds as fractions of `context.max_tokens`; use `complete.max_input_tokens(light=True)` to size what you send; let `BudgetExhausted` propagate with no state change.
- `assemble`: addendum counts against `max_tokens`; result is a suffix starting at a USER message, never splitting a call from its result, always including the newest message; oversized messages truncated in the view only.
- `TokenCounter` accepts `str | Sequence[Message]`.
- Store blocks under the stable tags in `api.BLOCK_TYPES` (never Python import paths); migrate rows already written with import paths; unknown tags decode to `OpaqueBlock` and re-encode unchanged. Persist `OperationResult` too.
- `append` is idempotent by message id (identical re-append is a no-op; same id with different content raises). `KeyError` for unknown conversation ids. `search` queries are plain text, higher score is better. Long-term memory is per mode.
- The store must tolerate a second process (the other mode) reading it (SQLite WAL is fine).
### Core
- `InferenceRequest.options` no longer exists: move provider options (thinking, temperature, tool_choice, ...) into adapter constructor configuration, optionally per model id.
- `ModelInfo`: `context_window` may be `None` (no silent default); fill the price fields from configuration.
- Set `Reasoning.model` on produced blocks and drop reasoning state from other models. Set `ToolCall.raw_arguments` when arguments do not parse. Raise `ContextOverflowError` for prompt-too-long errors. Map Anthropic `refusal` and OpenAI `content_filter` to `StopReason.REFUSAL`.
- `count_tokens` is optional (`int | None`). `stream` must support `aclose()`. Render `OperationResult` and the new tool statuses as text.
### Tools
- `ToolSpec.timeout` (None = runtime default, `math.inf` = unlimited). Pass the environment through to subprocesses (`PERMISSION_CEILING_ENV`).
- `progress()` is thread-safe, and `progress("")` is a silent heartbeat; add tests.
- Module configuration goes through `factory(options, paths: InstancePaths)`; give each module a factory.
### TUI
- Handle `ModelSelected`, `MessageQueued.message_id`, `PermissionResolved.call_id`, `StopStalled.call_ids`, `ToolBackgrounded` (no op id), `NOT_STARTED`, `MessageCommitted.stop` (show refusals distinctly), and `ModelRetry` (discard deltas).
- Remove the "always allow" choice. Add a per-operation cancel control (`cancel_operation`), show `queued_messages` from the snapshot, end `run()` when `events()` ends. Use paginated `history`.

## End-to-end check (last step)
Add a small `kayden` entry point (`python -m kayden`) that builds an instance from an `InstanceConfig`: SqliteMemory, Anthropic Core, ShellFilesTools rooted at the current directory, and the TUI. Do not run live API tests unless `ANTHROPIC_API_KEY` is set. Describe in `docs/decisions.md` how to start it. Without a key, run it against the scripted fakes to prove the wiring.