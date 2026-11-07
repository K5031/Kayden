# Agent Note: Instance config file

Status: implemented — modules and per-instance runtime settings come from a TOML file

## Problem

The launcher has to know which Core, Memory, Tools and Frontend to build, and the runtime
settings that belong to one instance (model, maintenance model, permission policy, budget)
need a home that persists between launches. Command-line flags alone cannot carry a budget
limit reliably: usage persists in the data directory, so a limit given per launch could
change or be forgotten. Contract issue 8.3 asked where these settings live.

## Decision

A TOML file per instance: `--config FILE` (must exist), else `<data dir>/config.toml`
(optional). Read with stdlib `tomllib` in `src/kayden/__main__.py`.

```toml
[core]                      # also [memory], [tools], [frontend]
factory = "kayden.core.openai_compat:factory"
options = { profile = "deepseek" }

[runtime]
model = "deepseek-flash"
maintenance_model = "deepseek-flash"
permissions = "ask"

[runtime.budget]            # the Budget fields; unset = unlimited
max_cost_usd = 5.0
```

- **Modules.** Each section takes `factory` and `options`, which map onto `ModuleSpec`. A
  section left out gets its default factory (`DEFAULT_FACTORIES`: Anthropic Core, SQLite
  Memory, shell/file Tools, TUI).
- **Runtime.** Only per-instance settings: `model`, `maintenance_model`, `permissions`,
  `budget`. Tuning fields (retries, timeouts, system prompt) stay at their code defaults.
- **Precedence.** `--model`, `--maintenance-model` and `--permissions` override the file;
  `--mode`, `--activation` and `--conversation` are merged over the frontend options.
- **Strict.** Unknown sections or keys, wrong types and negative budgets stop startup with
  a one-line message naming the file.
- **Keys.** Not in the file: each Core reads its own environment variable and refuses a
  missing key at construction; `kayden` reports that as one line.

Reference: `docs/decisions.md`, "Running Kayden"; README, "Config file".

## Alternatives considered

- **A `--core` flag.** Covers only the Core, and nothing persists per instance.
- **Modules only, no `[runtime]` section.** Leaves budgets unsettable and makes the
  permission policy depend on how the instance was launched.
- **Model under `[core]`.** Closer to the Core it depends on, but the model is runtime
  state (`select_model`, maintenance model, price checks).
- **Every `RuntimeConfig` field in the file.** Tuning knobs nobody needs; they can be added
  to `[runtime]` later without breaking existing files.
- **Writing a full default config on first run.** The copy goes stale when defaults change.
- **A launcher API-key check.** Provider-specific; the Cores check their own keys.

## Consequences

- The file is a direct serialization of `InstanceConfig` (spec 16.1 lists modules as
  factory paths), so adding a module never touches the launcher.
- A typo cannot silently run an instance without its budget.
- Defaults live in one place (code), so a missing file still runs, but they are visible
  only in docs and code: no command prints the effective config.
- The system prompt is not configurable; `system_prompt` or `system_prompt_file` in
  `[runtime]` is the place for it.
- The tools `root` defaults to the current directory only when `[tools]` has no options.
