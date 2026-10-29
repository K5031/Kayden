# Kayden

Kayden is a modular AI agent application. A runtime owns all orchestration (modes,
activation, budgets, permissions, background operations, the event log) and drives four
swappable modules through the contracts in `src/kayden/api.py`:

- **Core**: model inference (one LLM provider per implementation).
- **Memory**: the conversation archive, context window and long-term memory.
- **Tools**: the actions the agent can take.
- **Frontend**: the user interface.

Modules never call each other; the runtime connects them. `SPEC.md` is the product spec.

## Status

Early MVP, version 0.0.0. The runtime and one implementation of each module exist and
pass the offline test suite. No release has been made and the API may still change.

## Running

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```
export ANTHROPIC_API_KEY=...
cd <scratch directory>        # shell/file tools are rooted here
uv run --project <kayden checkout> python -m kayden
```

Options: `--mode session|continuous`, `--activation user_controlled|agent_controlled`,
`--conversation ID`, `--model MODEL_ID`, `--maintenance-model MODEL_ID`,
`--permissions ask|allow_read_only|deny` and `--data-dir DIR` (default `$KAYDEN_DATA_DIR`
or `~/.kayden`). Without `--mode` the TUI asks at startup.

**Run it in a scratch directory.** The agent's shell and file tools use the current
directory as their working directory, but they are not a sandbox: commands can reach
anything your user account can. Keep `--permissions ask` (the default) unless you trust
what the agent will do.

**Core choices.** `python -m kayden` always uses the Anthropic Core
(`kayden.core.anthropic`). The OpenAI-compatible Core (`kayden.core.openai_compat`, for
OpenAI or any server speaking the chat completions API) is selected by building an
`InstanceConfig` with `core=ModuleSpec("kayden.core.openai_compat:factory", {...})` and
calling `kayden.runtime.run_instance`; see `src/kayden/__main__.py` for the shape.

## Layout

```
src/kayden/api.py            module contracts (normative)
src/kayden/runtime/          orchestration, budgets, operations, event log, instance lock
src/kayden/core/             Anthropic and OpenAI-compatible Cores
src/kayden/memory_sqlite/    Memory on SQLite (FTS5)
src/kayden/tools/            shell and file Tools
src/kayden/frontends/        Textual TUI
tests/                       runtime, scenario and module tests; fakes/ and conformance/
docs/                        decisions, contract issues, plan; process/ holds build briefs
```

## Tests

```
uv run pytest
uv run ruff check
```

The suite runs offline against scripted fakes. The live Core conformance tests are skipped
unless `KAYDEN_LIVE_TESTS=1` is set together with `ANTHROPIC_API_KEY` (Anthropic) or
`KAYDEN_OPENAI_MODEL` (OpenAI-compatible; `OPENAI_API_KEY` and `KAYDEN_OPENAI_BASE_URL` as
the server needs).

## Limits

- POSIX only: the instance lock uses `fcntl` and the shell tool uses process groups.
- The live provider tests have not been run yet, so neither Core has been checked
  against a real API. The end-to-end test uses the real Memory, Tools, runtime and TUI
  with a scripted Core.
- Known spec and contract issues are recorded in `docs/contract-issues.md`.

## License

MIT; see `LICENSE`.
