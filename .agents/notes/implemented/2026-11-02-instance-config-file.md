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
(optional). Read with stdlib `tomllib` in `src/kadence/__main__.py`.

```toml
[core]                      # also [memory], [tools], [frontend]
factory = "kadence.core.openai_compat:factory"
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
  Memory, shell/file Tools, TUI). A module chosen in the app by id replaces the one named
  here (see [swappable modules](2026-11-08-swappable-modules-in-app.md)).
- **Runtime.** Only per-instance settings: `model`, `maintenance_model`, `permissions`,
  `budget`, and the system prompt. Tuning fields (retries, timeouts) stay at their code
  defaults.
- **System prompt.** `system_prompt` (inline) or `system_prompt_file` (a path relative to
  the config file, read at startup) replaces the default prompt; setting both, an empty
  prompt or a missing file stops startup. The runtime still appends its Continuous-mode
  instructions.
- **Effective config.** `kadence --show-config` prints the config the instance would run
  with as TOML (written with `tomli-w`) and exits without building any module: defaults
  filled in, the default system prompt in full, values under keys that name a key, token,
  secret or password redacted. Unset settings that only the Core resolves (the model) are
  listed as comments.
- **Precedence.** Settings saved through `RuntimeAPI.update_settings`
  (`<data dir>/settings.toml`, see [settings](2026-11-07-settings-for-non-technical-users.md)),
  module choices among them, override the file; `--model`, `--maintenance-model` and `--permissions` override both;
  `--conversation` is merged over the frontend options.
- **Strict.** Unknown sections or keys, wrong types and negative budgets stop startup with
  a one-line message naming the file.
- **Keys.** Not in the file: each Core reads its own environment variable, then the OS
  keychain, and refuses a missing key at construction. That one-line message, like any
  module that rejects its options, opens the frontend's settings with it.

Reference: `.agents/decisions.md`, "Running Kadence"; user docs: `docs/settings.md`.

## Alternatives considered

- **A `--core` flag.** Covers only the Core, and nothing persists per instance.
- **Modules only, no `[runtime]` section.** Leaves budgets unsettable and makes the
  permission policy depend on how the instance was launched.
- **Model under `[core]`.** Closer to the Core it depends on, but the model is runtime
  state (`select_model`, maintenance model, price checks).
- **Every `RuntimeConfig` field in the file.** Tuning knobs nobody needs; they can be added
  to `[runtime]` later without breaking existing files.
- **Writing a full default config on first run.** The copy goes stale when defaults change;
  `--show-config` prints the current defaults on demand instead.
- **Appending the configured prompt to the default.** Safer for the built-in tool
  instructions, but leaves no way to change or remove default wording; replacing it, with
  `--show-config` showing the default to start from, keeps full control.
- **A hand-written TOML writer for `--show-config`.** Stdlib reads TOML but cannot write it;
  `tomli-w` is the established writer and handles nested option tables and multiline
  strings.
- **A launcher API-key check.** Provider-specific; the Cores check their own keys.

## Consequences

- The file is a direct serialization of `InstanceConfig` (spec 16.1 lists modules as
  factory paths), so adding a module never touches the launcher.
- A typo cannot silently run an instance without its budget.
- Defaults live in one place (code), so a missing file still runs; `--show-config` makes
  them visible.
- A replaced system prompt drops the default's explanation of background operations,
  recall and peek_other_mode unless the user keeps it; the tools' own descriptions remain.
- `tomli-w` is one more dependency.
- The tools `root` defaults to the current directory only when `[tools]` has no options.
