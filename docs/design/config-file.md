# Instance config file

Status: built (2026-11-02). Code: `src/kayden/__main__.py`. Reference: `decisions.md`,
"Running Kayden"; README, "Config file".

## Problem

`python -m kayden` hardcoded the Anthropic Core, so the DeepSeek profile could only be
used from a hand-written Python script. Runtime settings (model, permissions, budget) had
no defined home (contract issue 8.3), and a budget limit could not be set at all from the
command line.

## Design

A TOML file per instance: `--config FILE` (must exist), else `<data dir>/config.toml`
(optional). Read with stdlib `tomllib`.

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
  section left out gets its default factory (Anthropic, SQLite Memory, shell/file Tools,
  TUI). The defaults live in code (`DEFAULT_FACTORIES`).
- **Runtime.** Only per-instance settings: `model`, `maintenance_model`, `permissions`,
  `budget`. Tuning fields (retries, timeouts, system prompt) stay at their defaults.
- **Precedence.** `--model`, `--maintenance-model` and `--permissions` override the file;
  `--mode`, `--activation` and `--conversation` are merged over the frontend options.
- **Strict.** Unknown sections or keys, wrong types and negative budgets stop startup with
  a one-line message naming the file.
- **Keys.** Not part of the design: each Core reads its own environment variable and
  refuses a missing key at construction. The launcher knows no provider.

## Why

- The spec already lists modules as factory import paths (16.1), so the file is a direct
  serialization of `InstanceConfig`; adding a module never touches the launcher.
- One file per data directory matches the spec's per-instance configuration (section 10).
- The budget limit belongs next to the usage it limits: usage persists in the data
  directory, so a limit given only on the command line could change or vanish per launch.
- Strict validation means a typo can't silently run an instance without its budget.
- Defaults stay in code: one source, and a missing file still runs.

## Rejected

- **A `--core` flag.** Covers only the Core and nothing persists per instance.
- **No `[runtime]` section** (the first version). Left budgets unsettable and made the
  permission policy depend on how the instance was launched.
- **Model under `[core]`.** Closer to the Core it depends on, but the model is runtime
  state (`select_model`, maintenance model, price checks), so it stays in `[runtime]`. A
  model the Core doesn't offer still fails at startup.
- **All `RuntimeConfig` fields in the file.** Tuning knobs nobody needs yet; they can be
  added to `[runtime]` without breaking existing files.
- **Writing a full default config on first run.** The copy would go stale when defaults
  change.
- **A launcher API-key check.** Provider-specific; the Cores check their own keys.

## Open questions

- A missing key ends startup with a traceback rather than a one-line message.
- Defaults are visible only in docs and code; a command that prints the effective config
  would make them discoverable.
- The system prompt is not configurable (`system_prompt` or `system_prompt_file` in
  `[runtime]` would be the place).
- The tools `root` defaults to the current directory only when `[tools]` has no options.
