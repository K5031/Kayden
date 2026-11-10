# Development

## Architecture

A **runtime** owns everything about how the agent runs: conversations, waking up after background work, permissions,
budgets, background tasks, settings and the event log. It drives four swappable
**modules** through the contracts in `src/kayden/api.py`:

| Module | Provides | Built in |
|---|---|---|
| Core | Model inference, one provider per implementation | Anthropic; OpenAI-compatible (OpenAI, DeepSeek) |
| Memory | Conversation archive, context, long-term memory | SQLite (full-text search) |
| Tools | The actions the agent can take | Shell commands and files |
| Frontend | The user interface | Terminal app (Textual); desktop app (PySide6) |

Modules never call each other; only the runtime talks to them. Each module is a factory,
`factory(options, paths)`, registered as a Python entry point, so a new one needs no change
to the runtime; `config.toml` can also name one by its import path,
`"package.module:factory"`.

Runtime settings (model, budget, permissions, instructions, the folder for new chats) are
read by the runtime and apply live; the runtime hands each conversation's folder to the
Tools (`Tools.use_folder`) when it opens it. Module settings (which module, the API key,
module options) are passed to factories, so they apply when Kayden restarts: `RuntimeAPI.restart()`
shuts the instance down and the launcher (`kayden`, or the window launcher) builds it again in
the same process. When Core, Memory or Tools cannot be built, the frontend runs on a setup
runtime (`runtime/setup.py`) that only shows and saves settings.

## Writing a module

Declare the factory as an entry point in your package's `pyproject.toml`, in the group for
its kind. The name is the module's id; once the package is installed in Kayden's
environment, the module is listed in every frontend's settings:

```toml
[project.entry-points."kayden.frontends"]   # or kayden.cores, kayden.memories, kayden.tools
my-ui = "my_package.ui:factory"
```

Optional attributes on the factory: `display_name` (shown instead of the id), `key_name`
for a Core (the environment variable and keychain entry of its API key, which the settings
ask for), and for a frontend `kind`: `"terminal"` (started by `kayden`, the default) or
`"window"` (run by the window launcher, `python -m kayden.window`), and `event_loop`: a
callable that runs a coroutine on the loop the frontend needs, like `asyncio.run` (the
default). The desktop app names Qt's; a GTK or web frontend names its own. An entry point whose
package does not import is not listed. Launchers may pass a frontend the options `start`,
`conversation_id` and `model`.

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
src/kayden/__main__.py    the `kayden` command: config, saved settings, restarts
tests/                    tests; fakes/ and conformance/ hold shared test modules
SPEC.md                   product spec
.agents/                  design records (see below)
```

## Working on the code

`uv tool install --editable` runs your checkout as it is, so code changes take effect the
next time you start `kayden`. With `"Kayden[desktop]"` and `--python 3.13`, the same
install also has the desktop app (see [Desktop app](desktop-app.md#install)). After a
change to dependencies or entry points in `pyproject.toml`, reinstall:
`uv tool install --editable --reinstall .`. Without installing, `uv run kayden` works from
the checkout.

## Tests

```bash
uv run pytest
uv run ruff check
```

The suite runs in parallel (pytest-xdist) and takes about 10 seconds; add `-n 0` to run it in
one process, for example under a debugger. It runs offline against fakes. Tests against real providers are skipped unless
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
`pyproject.toml`. The bundle installs Kayden itself (`requires = [".[desktop]"]`), so its
dependencies come from `[project]` and its modules are found through their entry points.

A package for the Linux you build on (here an Arch `.pkg.tar.zst`) uses the system Python,
which must also run Briefcase and have its headers (`Python.h`) installed. The script runs
Briefcase and adds `kayden` and `kd` to the package as links to its program, which then
starts the terminal app (Briefcase itself makes one program per app):

```bash
packaging/linux-system.sh
```

A Flatpak needs `flatpak-builder`; a macOS `.dmg` must be built on a Mac:

```bash
uvx --python 3.13 briefcase package linux flatpak
```

```bash
uvx --python 3.13 briefcase package macOS
```

The package lands in `dist/`. `build/`, `dist/` and `logs/` are Briefcase output and are not
committed. The package needs a `CHANGELOG` file (without an extension: Briefcase's Arch
template looks for exactly that name).

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
- Commands are sandboxed only on Linux with bubblewrap; elsewhere, and whenever the user
  allows a call outside the work folder, the shell runs with the user's full rights.
