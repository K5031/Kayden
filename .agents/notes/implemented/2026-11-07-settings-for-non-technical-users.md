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
| Provider | "Claude", "DeepSeek", "OpenAI", or any installed Core | The Core module |
| API key | A password field, for the chosen provider | The OS keychain, under the key's variable name |
| Model, maintenance model | A list with names and prices (`list_models()`) | `model`, `maintenance_model` |
| Spending limit | "$10 in total", with spending so far (`snapshot().usage`) | `Budget` |
| Permissions | "Ask me first" / "Read-only freely" / "Nothing" | `PermissionPolicy` |
| Instructions | A text box, prefilled with the built-in prompt | `system_prompt` |
| Work folder | A folder picker | the Tools `root` option |
| Memory, Tools, Interface | A list of the installed modules | The Memory, Tools and Frontend modules |

Model, maintenance model, spending limit, permissions and instructions are runtime
settings and apply live. Provider, key, work folder and the other modules are module
settings: saved at once, applied at the next start
([swappable modules](2026-11-08-swappable-modules-in-app.md)). Other module options
(profiles, prices, context windows) are not user settings. They ship built in (the DeepSeek
profile; `_PRICES` in the Anthropic Core); `config.toml` stays available for advanced
overrides.

### The runtime owns them

- `RuntimeAPI.settings()` returns the current settings and, per module kind, the installed
  modules with the running and the chosen one (`ModuleChoice`, `ModuleInfo`: id, name, key
  name, whether a key is available).
- `RuntimeAPI.update_settings(SettingsUpdate)` applies a partial change: it validates
  everything, stores a key, saves, then applies, so a failing part changes nothing
  (`ValueError`; a failed save restores the keychain entry). It emits `SettingsChanged`,
  and `ModelSelected` when the model changes. Calls are serialized by a lock.
- Model, maintenance model, budget, permissions and system prompt apply in any state, from
  the next inference. Permissions are clamped to any inherited ceiling, and the exported
  ceiling follows the new policy.
- Module choices, the key and the work folder are saved in any state and build nothing;
  `restart()` applies them. When the modules cannot be built, a setup runtime still shows
  and saves the settings, so a provider without a key yet can be configured in the app.
- Before `start()`, the Core is asked for its models only when a model must be checked.

### Confirmation and logging

Frontends must ask the user to confirm before loosening permissions or raising a budget
limit (SPEC 16.5, `update_settings` docstring); the runtime cannot tell whether a person
confirmed. Each change is a `settings` record in the event log with the changed fields, the
new values except the key and prompt text, and `loosened` listing changes that needed
confirmation.

### Keys

`update_settings` writes a key to the OS keychain (`keyring`; service `KEYCHAIN_SERVICE`,
entry = `ModuleInfo.key_name`, e.g. `ANTHROPIC_API_KEY`) and never to a file or the log.
Cores read the environment first, then the keychain (`core/_common.stored_key`), so keys
exported in a shell profile keep working.

### Persistence and startup

`update_settings` merges the changed fields into `<data dir>/settings.toml`, replaced
atomically; module choices go under `[modules]`. `kayden` builds the instance from
`config.toml`, then `settings.toml`, then command-line flags, each overriding the previous;
`--show-config` notes when saved settings apply. A chosen module keeps `config.toml`'s
options when it is the module named there, so switching back keeps them.

Code: `api.py` (`Settings`, `SettingsUpdate`, `ModuleChoice`, `ModuleInfo`,
`SettingsChanged`, `KEYCHAIN_SERVICE`), `runtime/orchestrator.py` (`settings`,
`update_settings`), `runtime/modules.py`, `runtime/settings.py`, `__main__.py`. Tests:
`tests/test_settings.py`, `tests/test_modules.py`, `tests/test_e2e.py`, the Core suites.

## Alternatives considered

- **A host layer above the runtime** (the grown `kayden` launcher) owning settings, keys
  and restarts, with a second Settings API handed to frontends. Keeps the runtime free of
  settings, but adds a layer and a second contract for every frontend. The runtime already
  persists state in the data directory, and `restart()` hands the launcher only what to
  start next.
- **Frontends edit `config.toml` directly.** Ties every frontend to the file format and to
  running on the same machine; gives no validation and no live apply.
- **Rebuilding the Core or Tools live** for a provider, key or work-folder change, once
  nothing runs. Needed quiescence checks and rollback, and a second rule beside "module
  settings apply at the next start"; see the
  [swappable modules note](2026-11-08-swappable-modules-in-app.md).
- **Saving every setting, not only changed ones.** Would freeze defaults and hide later
  edits to `config.toml` behind `settings.toml`.
- **Keys in the settings file.** Plain-text secrets in the data directory; the OS keychain is
  the established place.
- **Module options in the settings screen** (profiles, prices). Meaningless to the target
  user and easy to get wrong.

## Consequences

- Every frontend gets settings through the contract it already uses. The TUI has a
  settings screen (F2 or `/settings`, `SettingsScreen` in `frontends/tui.py`): it sends only
  the fields that changed, and a `ConfirmScreen` asks before loosening permissions,
  raising or removing the spending limit, or switching Memory. It edits only the cost
  limit; other budget dimensions stay as configured. The desktop app's Settings window
  does the same.
- The runtime is larger: settings persistence and module discovery.
- Any frontend, and whatever drives it, can loosen permissions or raise the budget; the
  confirmation rule binds well-behaved frontends only, and the event log records it.
- After a provider switch and restart, earlier reasoning comes from another model and is
  not replayed (M10); the conversation continues without it.
- Headless Linux machines may have no keychain backend; there `update_settings` refuses a
  key with a message to set the environment variable instead.
- Built-in Claude prices are dated 2026-10-06 and nothing refreshes them; Haiku 5.5 uses its
  higher (over 100K tokens) rate.
- `keyring` is one more dependency.
