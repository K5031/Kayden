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

Early MVP, version 0.0.0. The runtime and at least one implementation of each module
exist and pass the offline test suite. No release has been made and the API may still change.

## Running

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```
uv tool install --editable <kayden checkout>   # once: puts `kayden` on your PATH
export ANTHROPIC_API_KEY=...
cd <scratch directory>        # shell/file tools are rooted here
kayden
```

The editable install runs the checkout as it is, so code changes take effect without
reinstalling; a dependency change in `pyproject.toml` needs `uv tool install --editable
--reinstall <kayden checkout>`. Without installing, `uv run --project <kayden checkout>
kayden` (or `python -m kayden`) works too.

Without uv, install it with pip into a virtual environment (distributions such as Arch
refuse system-wide `pip install`):

```
python -m venv ~/.venvs/kayden
~/.venvs/kayden/bin/pip install git+https://github.com/K5031/Kayden.git
cd <scratch directory>
~/.venvs/kayden/bin/kayden
```

From a checkout, `pip install -e <kayden checkout>` makes code changes take effect without
reinstalling. pip does not use `uv.lock`, so it may pick newer dependency versions than the
ones tested.

Options: `--config FILE`, `--mode session|continuous`,
`--activation user_controlled|agent_controlled`, `--conversation ID`, `--model MODEL_ID`,
`--maintenance-model MODEL_ID`, `--permissions ask|allow_read_only|deny` and
`--data-dir DIR` (default `$KAYDEN_DATA_DIR` or `~/.kayden`). Without `--mode` the TUI asks
at startup.

**Run it in a scratch directory.** The agent's shell and file tools use the current
directory as their working directory, but they are not a sandbox: commands can reach
anything your user account can. Keep `--permissions ask` (the default) unless you trust
what the agent will do.

**Config file.** A TOML file configures the instance: `--config FILE`, else
`<data dir>/config.toml`. Each of `[core]`, `[memory]`, `[tools]` and `[frontend]` takes a
`factory` ("package.module:factory") and `options`; a module left out gets its default
(Anthropic Core, SQLite Memory, shell/file Tools rooted at the current directory, TUI).
`[runtime]` takes `model`, `maintenance_model`, `permissions`, a system prompt and a
`[runtime.budget]` table (`max_tokens`, `max_cost_usd`, `max_duration_s`,
`max_tool_calls`); command-line flags override it. DeepSeek with a $5 limit, the key in
`DEEPSEEK_API_KEY`:

```toml
[core]
factory = "kayden.core.openai_compat:factory"
options = { profile = "deepseek" }

[runtime]
model = "deepseek-flash"

[runtime.budget]
max_cost_usd = 5.0
```

**System prompt.** `system_prompt = "..."` or `system_prompt_file = "prompt.md"` (relative
to the config file) in `[runtime]` replaces the built-in prompt; Continuous Mode still adds
its instructions on how to idle. The built-in prompt explains background operations,
`recall` and `peek_other_mode`, so start from it: `kayden --show-config` prints it.

**Saved settings.** In the TUI, press F2 (or type `/settings`) to change the provider, API
key, model, spending limit, permissions, instructions and work folder; loosening permissions
or raising the limit asks for confirmation. Any frontend can do this through the runtime;
the changes are saved to
`<data dir>/settings.toml` and override `config.toml` (command-line flags override both).
API keys set that way go to the OS keychain, never to a file. Keys exported in the
environment still work and take precedence.

**Effective config.** `kayden --show-config` prints the config the instance would run with,
defaults filled in and secrets redacted, as TOML you can copy into a config file. It builds
no module and needs no API key.

**API keys.** Each Core reads its key from the environment (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `DEEPSEEK_API_KEY`) and refuses to start without one. An `api_key` in
`[core]` options also works but keeps the key in plain text; if you do that, make the file
private (`chmod 600`) and never commit it.

## Layout

```
src/kayden/api.py            module contracts (normative)
src/kayden/runtime/          orchestration, budgets, operations, event log, instance lock
src/kayden/core/             Anthropic and OpenAI-compatible Cores
src/kayden/memory/           SQLite Memory (FTS5)
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
the server needs) or `DEEPSEEK_API_KEY` (DeepSeek profile; `KAYDEN_DEEPSEEK_MODEL` defaults
to `deepseek-flash`).

## Limits

- POSIX only: the instance lock uses `fcntl` and the shell tool uses process groups.
- The OpenAI-compatible Core has been checked live against DeepSeek (results in
  `docs/decisions.md`); the Anthropic Core has not been run against the real API yet.
  The end-to-end test uses the real Memory, Tools, runtime and TUI with a scripted Core.
- A missing API key ends startup with a Python traceback rather than a short message.
- Known spec and contract issues are recorded in `docs/contract-issues.md`.

## License

MIT; see `LICENSE`.
