# Agent Note: Swappable modules from within the app

Status: proposed — every module (Core, Memory, Tools, Frontend) is chosen from the installed ones in the app, applied at the next start

## Problem

Kayden's architecture makes Core, Memory, Tools and Frontend swappable, and users should be
able to write their own modules, frontends included. Today a different module means editing
`[core]`, `[memory]`, `[tools]` or `[frontend]` in `config.toml` with a Python import path,
which a non-technical user cannot do, and Kayden cannot list what is installed. The settings
screen offers only a fixed list of providers, and the
[settings note](../implemented/2026-11-07-settings-for-non-technical-users.md) records the
frontend as "not a setting" because a frontend cannot replace itself while it runs.

## Proposal

### Rule

Choosing **which module** to use is a setting like any other, changed in the app's settings,
saved, and applied at the next start of Kayden. The running instance keeps its modules until
then, so the user keeps working without interruption. Settings **of** the running modules
(API key, work folder) and runtime settings (model, prompt, budget, permissions) keep
applying live.

### Discovery: entry points

Installed modules are found through Python entry points, the standard plugin mechanism
(`importlib.metadata.entry_points`), one group per kind: `kayden.cores`, `kayden.memories`,
`kayden.tools`, `kayden.frontends`. An entry point's name is the module's id; its target is
the factory, `factory(options, paths)` as today. Installing a package that declares one is
all a user-made module needs. Kayden's own modules register the same way in
`pyproject.toml`, so they are not special:

| Group | Built-in ids |
|---|---|
| `kayden.cores` | `anthropic`, `deepseek`, `openai` |
| `kayden.memories` | `sqlite` |
| `kayden.tools` | `shell-files` |
| `kayden.frontends` | `tui`, `desktop` |

A provider is a Core entry: the DeepSeek and OpenAI profiles get their own factories
(`kayden.core.openai_compat:deepseek_factory`, ...), so the separate `Provider` list in
`__main__.py` and `RuntimeConfig.providers` go away. A factory may carry `display_name` and,
for Cores, `key_name` attributes; without them the id is shown and no key is asked for.

### Settings

- `Settings` gains `modules`: for each kind, the id running now, the id that will run after
  a restart, and the installed choices (id, display name, and for Cores whether a key is
  set). `Settings.provider` and `providers` are replaced by the Core entries.
- `SettingsUpdate` gains `modules` (kind → id). An unknown id raises `ValueError`. The
  change is saved to `settings.toml` under `[modules]`, emits `SettingsChanged`, and builds
  nothing.
- `api_key` stays live and applies to the running Core when it belongs to it; for a Core
  that only runs after the restart, it is just stored.
- The work folder stays live (it is an option of the running Tools).
- The live provider switch (`update_settings(provider=...)` rebuilding the Core between
  turns) is removed: changing the Core is a module change.
- Options that only a module understands stay in `config.toml`; the app does not edit them.

### Startup and restart

- `kayden` builds the instance from `config.toml`, then `settings.toml` (which now may name
  modules by id), then command-line flags, as today. An id that is no longer installed
  stops startup with a one-line message naming it.
- Frontends show "applies when you restart Kayden" for pending module changes, and offer
  **Restart now**: the frontend asks the launcher to restart, the instance shuts down as on
  quit (running tools end as by Stop), and the launcher re-executes itself with the same
  arguments (`os.execv`).
- A restart cannot always show the new frontend in the same place: a terminal frontend needs
  a terminal, a window frontend needs a desktop. `FrontendInfo` gains `kind`
  (`"terminal"` or `"window"`). Restart now is offered only when the pending frontend has
  the same kind as the running one; otherwise the frontend says it will be used the next time
  Kayden is started from a terminal or from the app menu. `kayden` and `kayden-desktop` each
  start the frontend saved in settings when its kind matches how they were launched, and
  their own frontend otherwise.

### Memory changes

A different Memory module is a different archive: conversations and long-term memory of the
old one are not visible after the restart, and come back when the user switches back. The
settings screen says so before saving a Memory change.

## Alternatives considered

- **Swapping modules live, while running.** A frontend cannot replace itself, a different
  Memory under an open conversation has none of its messages, and the live Core swap needed
  quiescence checks and rollback. Restart-applied changes need none of that.
- **Typing an import path in the app** (`package.module:factory`). Works for any module
  with no registration, but non-technical users cannot know paths, and a typo is found only
  at the next start.
- **A plugin folder in the data directory** (drop `.py` files into `~/.kayden/plugins`).
  No packaging needed, but Kayden would execute whatever lands in that folder and has no
  dependency handling; entry points make installing the trust decision and resolve
  dependencies.
- **A supervisor process that restarts the instance.** Can restart without the launcher
  re-executing itself and could open a terminal for a terminal frontend, but adds a process
  and a protocol; `os.execv` is enough for restarts of the same kind.
- **Keeping providers as a separate list beside Core modules.** Two ways to pick a Core
  confuse users and duplicate code; a provider is a Core with preset options.

## Acceptance criteria

- A package that declares a `kayden.frontends` (or other) entry point appears in the
  settings screen of every frontend after installation, with no config editing.
- Choosing a different module saves it, shows that it applies after a restart, and leaves
  the running instance and conversation untouched.
- Restart now brings Kayden back with the new modules and the same conversation, when the
  frontend kind allows it; otherwise the next launch of that kind uses the new frontend.
- A module that is no longer installed stops startup with a one-line message.
- API key and work folder still apply without a restart.
- Existing `config.toml` files with import paths keep working.

## Risks

- **Plugins are code.** An installed module runs with the user's permissions; listing only
  installed packages leaves the trust decision to installation, which users may not treat as
  one. The docs say so.
- **Losing the live provider switch.** Switching provider now needs a restart; a few seconds
  for the user, and the conversation continues after it.
- **Memory switches look like lost data.** The warning before saving must be clear.
- **Overlap with other proposals.** The [autonomy note](../implemented/2026-11-07-autonomy-into-sessions.md)
  removes modes and changes Memory's contract, and the
  [desktop app note](2026-11-07-desktop-app.md) adds `kayden-desktop`; this note's
  settings and launcher changes must be built after or together with them.
- **The settings note changes.** Its "frontend is not a setting" and live provider switch
  become wrong once this is built; it is rewritten then.
