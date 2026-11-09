# Development

## Architecture

A **runtime** owns everything about how the agent runs: modes, activation, permissions,
budgets, background tasks, settings and the event log. It drives four swappable
**modules** through the contracts in `src/kayden/api.py`:

| Module | Provides | Built in |
|---|---|---|
| Core | Model inference, one provider per implementation | Anthropic; OpenAI-compatible (OpenAI, DeepSeek) |
| Memory | Conversation archive, context, long-term memory | SQLite (full-text search) |
| Tools | The actions the agent can take | Shell commands and files |
| Frontend | The user interface | Terminal app (Textual); desktop app (PySide6) |

Modules never call each other; only the runtime talks to them. A module is named in the
config by its import path, `"package.module:factory"`, so a new one needs no change to
the runtime.

## Layout

```
src/kayden/api.py         module contracts (normative)
src/kayden/runtime/       orchestration, budgets, operations, settings, event log, lock
src/kayden/core/          Cores
src/kayden/memory/        SQLite Memory
src/kayden/tools/         shell and file Tools
src/kayden/frontends/     terminal app; common.py holds what every frontend shares
src/kayden/frontends/desktop/  desktop app: viewmodel.py (no Qt), window.py, launcher.py
src/kayden_desktop/       the module a Briefcase bundle runs
src/kayden/__main__.py    the `kayden` command: config, saved settings, providers
tests/                    tests; fakes/ and conformance/ hold shared test modules
SPEC.md                   product spec
.agents/                  design records (see below)
```

## Working on the code

`uv tool install --editable` runs your checkout as it is, so code changes take effect the
next time you start `kayden`. After a dependency change in `pyproject.toml`, reinstall:
`uv tool install --editable --reinstall .`. Without installing, `uv run kayden` works from
the checkout.

## Tests

```bash
uv run pytest
uv run ruff check
```

The suite runs offline against fakes. Tests against real providers are skipped unless
`KAYDEN_LIVE_TESTS=1` is set together with `ANTHROPIC_API_KEY` (Anthropic),
`DEEPSEEK_API_KEY` (DeepSeek; `KAYDEN_DEEPSEEK_MODEL` defaults to `deepseek-flash`) or
`KAYDEN_OPENAI_MODEL` (OpenAI-compatible; `OPENAI_API_KEY` and `KAYDEN_OPENAI_BASE_URL` as
the server needs).

### The desktop app's event loop

The desktop app runs the runtime on qasync's Qt event loop. To run async tests on that
loop, one file at a time (a whole run creates hundreds of Qt loops and crashes):

```bash
KAYDEN_TEST_LOOP=qasync uv run pytest tests/test_runtime.py
```

On Python 3.15, `tests/test_commands.py` hangs and `tests/core/test_openai_compat.py`
crashes on this loop; on 3.13 every file passes.

## Desktop bundles

[Briefcase](https://briefcase.beeware.org/) builds them from `[tool.briefcase]` in
`pyproject.toml`, with a Python the bundle then carries:

```bash
uvx --python 3.13 briefcase build linux flatpak
uvx --python 3.13 briefcase package macOS
```

`build/` and `logs/` are Briefcase output and are not committed.

## Design records

`.agents/` holds working records written while building Kayden, for contributors and
coding agents rather than users:

| Path | Holds |
|---|---|
| `.agents/notes/` | Agent Notes: design decisions, the alternatives and their costs |
| `.agents/contract-issues.md` | Problems found in the spec and how each was settled |
| `.agents/decisions.md` | Implementation choices: defaults, formats, live test results |
| `.agents/plan.md` | Each specified behavior and the test that covers it |
| `.agents/process/` | The briefs earlier build phases followed |

`AGENTS.md` holds the rules for working on the code.

## Limits

- POSIX only: the instance lock uses `fcntl` and the shell tool uses process groups.
- The shell and file tools are not a sandbox.
