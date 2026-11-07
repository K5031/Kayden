# Agent Note: Settings for non-technical users

Status: implemented — the runtime owns user settings; every frontend changes them through `RuntimeAPI`

## Problem

Kayden should be usable by people who never open a terminal. Without this, every setting
lives in `~/.kayden/config.toml`, which names Python factories and nested option tables,
and every API key must be exported as an environment variable in a shell profile. A user who
wants a different provider, model, spending limit, permission level or set of instructions
has to edit files and restart, and no frontend can offer a settings screen.

## Decision

### The settings

The user settings, in the terms a settings screen shows (`api.Settings`):

| Setting | Shown as | Underneath |
|---|---|---|
| Provider | "Claude", "DeepSeek", "OpenAI" | The Core module and its options |
| API key | A password field, per provider | The OS keychain, under the key's variable name |
| Model, maintenance model | A list with names and prices (`list_models()`) | `model`, `maintenance_model` |
| Spending limit | "$10 in total", with spending so far (`snapshot().usage`) | `Budget` |
| Permissions | "Ask me first" / "Read-only freely" / "Nothing" | `PermissionPolicy` |
| Instructions | A text box, prefilled with the built-in prompt | `system_prompt` |
| Work folder | A folder picker | the Tools `root` option |

Module options (profiles, prices, context windows) are not user settings. They ship built in
(the DeepSeek profile; `_PRICES` in the Anthropic Core); `config.toml` stays available for
advanced overrides.

### The runtime owns them

- `RuntimeAPI.settings()` returns the current settings and the provider list
  (`ProviderInfo`: id, name, key name, whether a key is available).
- `RuntimeAPI.update_settings(SettingsUpdate)` applies a partial change: it validates
  everything, builds any replacement module, saves, then applies, so a failing part changes
  nothing (`ValueError`). It emits `SettingsChanged`, and `ModelSelected` when the model
  changes. Calls are serialized by a lock.
- Model, maintenance model, budget, permissions and system prompt apply in any state, from
  the next inference. Permissions are clamped to any inherited ceiling, and the exported
  ceiling follows the new policy.
- A provider, key or work-folder change rebuilds the Core or Tools (`load_module`, off the
  event loop) only when no cycle runs and no operation is outstanding, checked again just
  before the swap (`RuntimeStateError` otherwise). A new Core must list its models first, so
  a rejected key leaves the old provider in place and restores the keychain entry. The model
  becomes the requested one or the provider's first; the maintenance model is cleared unless
  set. The old module is closed.
- Memory and the frontend are not settings: a different Memory is a different archive, and a
  frontend cannot replace itself. Both stay in `config.toml`.
- Before `start()`, the Core is asked for its models only when a model must be checked, so a
  provider without a key yet can still be configured.

### Confirmation and logging

Frontends must ask the user to confirm before loosening permissions or raising a budget
limit (SPEC 16.5, `update_settings` docstring); the runtime cannot tell whether a person
confirmed. Each change is a `settings` record in the event log with the changed fields, the
new values except the key and prompt text, and `loosened` listing changes that needed
confirmation.

### Keys

`update_settings` writes a key to the OS keychain (`keyring`; service `KEYCHAIN_SERVICE`,
entry = `ProviderInfo.key_name`, e.g. `ANTHROPIC_API_KEY`) and never to a file or the log.
Cores read the environment first, then the keychain (`core/_common.stored_key`), so keys
exported in a shell profile keep working.

### Persistence and startup

`update_settings` merges the changed fields into `<data dir>/settings.toml`, replaced
atomically. `kayden` builds the instance from `config.toml`, then `settings.toml`, then
command-line flags, each overriding the previous; `--show-config` notes when saved settings
apply. The launcher owns the provider list (`PROVIDERS` in `__main__.py`); the entry matching
the configured Core carries its configured options, so switching back keeps them.

Code: `api.py` (`Settings`, `SettingsUpdate`, `ProviderInfo`, `SettingsChanged`,
`KEYCHAIN_SERVICE`), `runtime/orchestrator.py` (`settings`, `update_settings`),
`runtime/settings.py`, `runtime/config.py` (`Provider`), `__main__.py`. Tests:
`tests/test_settings.py`, `tests/test_e2e.py`, the Core suites.

## Alternatives considered

- **A host layer above the runtime** (the grown `kayden` launcher) owning settings, keys
  and restarts, with a second Settings API handed to frontends. Keeps the runtime free of
  settings, but adds a layer and a second contract for every frontend, and needs a restart
  for each provider change. The runtime can swap the Core or Tools itself under the rule
  `start()` uses, and it already persists state in the data directory.
- **Frontends edit `config.toml` directly.** Ties every frontend to the file format and to
  running on the same machine; gives no validation and no live apply.
- **Swapping modules while operations run.** Would need a hand-over of in-flight operations
  between Tools instances; waiting until nothing runs is simple.
- **Saving every setting, not only changed ones.** Would freeze defaults and hide later
  edits to `config.toml` behind `settings.toml`.
- **Keys in the settings file.** Plain-text secrets in the data directory; the OS keychain is
  the established place.
- **Module options in the settings screen** (profiles, prices). Meaningless to the target
  user and easy to get wrong.

## Consequences

- Every frontend gets settings through the contract it already uses. The TUI has a
  settings screen (F2 or `/settings`, `SettingsScreen` in `frontends/tui.py`): it sends only
  the fields that changed, and a `ConfirmScreen` asks before loosening permissions or
  raising or removing the spending limit. It edits only the cost limit; other budget
  dimensions stay as configured. The desktop app (a separate note, and the first graphical
  frontend) is still to be built.
- The runtime is larger: provider list, module rebuilding and settings persistence.
- Any frontend, and whatever drives it, can loosen permissions or raise the budget; the
  confirmation rule binds well-behaved frontends only, and the event log records it.
- After a provider switch, earlier reasoning comes from another model and is not replayed
  (M10); the conversation continues without it.
- Headless Linux machines may have no keychain backend; there `update_settings` refuses a
  key with a message to set the environment variable instead.
- Built-in Claude prices are dated 2026-10-06 and nothing refreshes them; Haiku 5.5 uses its
  higher (over 100K tokens) rate.
- `keyring` is one more dependency.
