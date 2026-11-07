# Agent Note: Settings for non-technical users

Status: proposed — the runtime owns user settings, and every frontend changes them through `RuntimeAPI`

## Problem

Kayden should be usable by people who never open a terminal. Today every setting lives in
`~/.kayden/config.toml`, which names Python factories and nested option tables, and every API
key must be exported as an environment variable in a shell profile. A user who wants a
different provider, model, spending limit, permission level or set of instructions has to
edit those files and restart. SPEC 16.5 also says "Configuration is read-only to frontends",
so no frontend can offer a settings screen; only the model can be changed (`select_model`).

## Proposal

### What the user sees

A settings screen in every frontend, in plain language:

| Setting | Shown as | Underneath |
|---|---|---|
| Provider | "Claude", "DeepSeek", ... | The Core module and its options |
| API key | A password field, per provider | Stored in the OS keychain |
| Model | A list with names and rough prices | `model`, `maintenance_model` |
| Spending limit | "$10 in total", with what is spent so far | `Budget` |
| Permissions | "Ask me first" / "Read-only freely" / "Nothing" | `PermissionPolicy` |
| Instructions | A text box, prefilled with the built-in prompt | `system_prompt` |
| Work folder | A folder picker | the Tools `root` option |

Module options (profiles, prices, context windows) are not user settings. They ship built
in, so a user never needs them; `config.toml` stays available for advanced overrides.

### The runtime owns settings

- **`RuntimeAPI.settings()`** returns the current settings and the choices a frontend
  needs to render them: providers with display names and whether each has a key, models
  with prices, permission levels, spending so far.
- **`RuntimeAPI.update_settings(changes)`** validates and applies a partial change, saves it,
  and emits `SettingsChanged` so every attached frontend refreshes. Invalid values raise
  `ValueError`; a change not allowed in the current state raises `RuntimeStateError`.
- **Applied live, in any state:** model, instructions (from the next request), spending
  limit, and permissions.
- **Applied by swapping a module, only when no cycle is running and no operation is
  outstanding** (the rule `start()` already follows): provider and key rebuild the Core from
  its factory, then the model list is refreshed; the work folder rebuilds Tools. No restart.
- **Confirmation.** Before loosening permissions or raising the spending limit, a frontend
  must ask the user to confirm and send the change only after they do. This is a rule for
  frontends: the runtime cannot tell whether a person confirmed. The runtime writes every
  settings change to the event log either way.
- **Not settings:** Memory (a different Memory is a different archive, so a different
  instance) and the frontend (it cannot replace itself). Both stay in `config.toml` and take a
  restart.

### Providers, keys and prices

- **Provider catalog.** Data passed in the instance configuration: display name, Core
  factory, options, keychain name. The runtime builds Cores from it with `load_module`, so it
  hardcodes no provider. `kayden` supplies the built-in catalog.
- **Keys.** `update_settings` writes a key to the OS keychain (`keyring` library) under the
  provider's keychain name and never to a file. Cores read their key from the environment
  first, then from the keychain, so the current setup keeps working.
- **Prices.** Each Core ships prices for its models, so the spending limit works without
  hand-typed prices.

### Persistence

The runtime saves user settings to `settings.toml` in the data directory, next to
`usage-<mode>.json`. At startup `kayden` reads `config.toml`, then `settings.toml`, then
command-line flags, each overriding the previous; `--show-config` shows the result.

### Contract changes

- SPEC 16.5: "Configuration is read-only to frontends" becomes "Frontends read and change
  user settings through `settings()` and `update_settings()`; module options are not user
  settings."
- `RuntimeConfig` ("read-only after start()") and `AgentConfig` docstrings follow.
- New: `RuntimeAPI.settings`, `RuntimeAPI.update_settings`, the `SettingsChanged` event, the
  provider catalog in the instance configuration, and a keychain fallback in each Core.

### Separate notes (not part of this one)

- A desktop app frontend that non-technical users can use. It comes first, before any web
  frontend.
- Installation without Python or a terminal.

## Alternatives considered

- **A host layer above the runtime** (the grown `kayden` launcher) owning settings, keys
  and restarts, with a second Settings API handed to frontends. Keeps the runtime free of
  settings, but adds a layer and a second contract for every frontend, and needs a restart
  for each provider change. The runtime can swap the Core or Tools itself under the same
  rule `start()` uses, and it already persists state (`usage-<mode>.json`, the event log) in
  the data directory, so the extra layer buys little.
- **Frontends edit `config.toml` directly.** Ties every frontend to the file format and to
  running on the same machine; gives no validation and no live apply.
- **Swapping modules while operations run.** Would need a hand-over of in-flight
  operations between Tools instances; waiting until nothing runs is simple and the user
  is changing settings, not working.
- **Keys in the settings file.** Plain-text secrets in the data directory; the OS keychain is
  the established place.
- **Module options in the settings screen** (profiles, prices). Meaningless to the target
  user and easy to get wrong; built-in defaults with `config.toml` for experts serve both.

## Acceptance criteria

- A user can pick a provider, enter a key, choose a model, set a spending limit, a
  permission level, instructions and a work folder from a frontend, with no file editing, no
  environment variables and no restart.
- A provider or work-folder change is refused while the agent is working and accepted once
  it is idle; the conversation continues on the new provider.
- Keys never land in a file Kayden writes.
- Every attached frontend sees a change through `SettingsChanged`.
- Loosening permissions or raising the spending limit asks the user to confirm first, in
  every frontend.
- Existing `config.toml` files and environment-variable keys keep working.

## Risks

- **Safety.** Any frontend can loosen permissions or raise the budget, so whatever drives a
  frontend can too; SPEC section 3 already notes that a malicious frontend could
  misrepresent user actions. The confirmation rule binds well-behaved frontends only; the
  event log records every change.
- **A larger runtime.** It gains the provider catalog, module rebuilding and settings
  persistence; module swapping must be tested against every state.
- **Reasoning across providers.** After a provider swap, earlier reasoning blocks come from
  another model and are not replayed (M10); the conversation continues without them.
- **Keychain availability.** Headless Linux machines may have no keychain backend; there
  the key must come from an environment variable, with a clear message.
- **Scope.** This goes beyond the v1 MVP and needs the graphical frontend to pay off for the
  target user.
