# Agent Note: Swappable modules from within the app

Status: proposed — every module (Core, Memory, Tools, Frontend) is chosen from the installed ones in the app, applied at the next start

## Problem

Kayden's architecture makes Core, Memory, Tools and Frontend swappable, and users should be
able to write their own modules, frontends included. Today a different module means editing
`[core]`, `[memory]`, `[tools]` or `[frontend]` in `config.toml` with a Python import path,
which a non-technical user cannot do, and Kayden cannot list what is installed. The settings
screen offers only a fixed list of providers, and the
[settings note](../implemented/2026-11-07-settings-for-non-technical-users.md) records the
frontend as "not a setting" because a frontend cannot replace itself while it runs. The
owner's main need is to change the frontend from inside the app: from the TUI to the
desktop app and back.

## Proposal

### Rule: runtime settings apply live, module settings at the next start

No special cases. A setting is a **runtime setting** if the runtime itself reads it while
running: model, maintenance model, system prompt, budget, permissions, autonomy. These apply
live, as today. A setting is a **module setting** if it is only passed into a module's
factory or read when the module is built: which module of each kind, the API key, the work
folder (the Tools `root`), profiles, prices, base URLs, compaction options. These are saved
at once and apply at the next start of Kayden; the running instance keeps its modules until
then, so the user keeps working without interruption.

So the API key and work folder are saved immediately but apply at the next start, like a
module choice. Nothing rebuilds a module while Kayden runs: the live provider switch and the
module rebuilding behind it (`_build`, `_close_module` and the quiescence checks in
`orchestrator.py`) are removed.

By the same criterion `[runtime].context_windows` (`RuntimeConfig.context_windows`), which
duplicates the Cores' per-model `context_window` option, belongs to the Core and is removed.

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

Only entry points that load (their package imports cleanly) are listed; one that fails is
logged and skipped. The desktop frontend's package imports PySide6 and qasync at the top,
so it is listed only where the `desktop` extra is installed on a Python it supports.

A provider is a Core entry: the DeepSeek and OpenAI profiles get their own factories
(`kayden.core.openai_compat:deepseek_factory`, ...), so the separate `Provider` list in
`__main__.py` and `RuntimeConfig.providers` go away. A factory may carry `display_name`;
Cores may carry `key_name`, and frontends `kind` (`"terminal"` or `"window"`, default
terminal). Without them the id is shown and no key is asked for.

### Settings

- `Settings` gains `modules`: for each kind, the id running now, the id that will run after
  a restart, and the installed choices (id, display name, and for Cores the key name and
  whether a key is set; for frontends the kind). `Settings.provider` and `providers` go.
- `Settings` also says whether saved module settings differ from the running instance
  (`restart_needed`), which frontend Restart now brings up (`restart_frontend`), and why
  startup failed (`startup_error`, see below). `work_folder` is the saved one.
- `SettingsUpdate` gains `modules` (kind → id); `provider` goes. An unknown id raises
  `ValueError`. The change is saved to `settings.toml` under `[modules]`, emits
  `SettingsChanged`, and builds nothing. A Core change also clears the saved model and
  maintenance model, so the new Core starts on its first model.
- `api_key` is stored for the chosen Core (the one in the same update, else the saved
  choice) and applies at the next start.
- Module settings save in any state: nothing waits for the agent to be idle.
- Options that only a module understands stay in `config.toml`; the app does not edit them.

### Startup

- `kayden` builds the instance from `config.toml`, then `settings.toml` (which may name
  modules by id), then command-line flags, as today. Import paths in `config.toml` keep
  working; such a module shows as "custom" and has no id.
- The frontend is built first, then Core, Memory and Tools. If they cannot be built because
  of module settings (no key for a Core that needs one, an id that is no longer installed,
  bad options), the frontend still opens, on a setup runtime: `settings()` reports the
  error in `startup_error`, module settings and the runtime settings that need no Core
  (budget, permissions, system prompt) can be saved, Restart now tries again, and every
  other command raises `RuntimeStateError`. A non-technical user fixes it in the app
  instead of facing a traceback. A frontend that itself cannot be built falls back to the
  launcher's own one, which then shows the error.
- First run is that case: no key yet. The first-run key entry (the TUI's settings screen,
  the desktop app's welcome) happens on the setup runtime, before the instance is built,
  so the key is in place when the Core is constructed.

### Restart

- Frontends show "applies when you restart Kayden" for pending module settings and offer
  **Restart now** (`RuntimeAPI.restart()`): the instance shuts down as on quit (running
  tools end as by Stop), the frontend's `run()` returns as the event stream ends, and the
  launcher starts again with the saved settings in the same process, reopening the open
  conversation (unless the Memory changed).
- A restart cannot always show the new frontend in the same place. `FrontendInfo` gains
  `kind`. From a terminal frontend to a window frontend, Restart now quits the TUI and
  launches the desktop app (`kayden-desktop`) as a detached process on the same data
  directory and conversation, when a display is available; without one, the TUI says the
  desktop app will be used the next time Kayden is started from the app menu. From a window
  frontend to a terminal one, the app cannot open a terminal: the desktop app says the TUI
  will be used the next time `kayden` is run, and Restart now restarts the desktop app for
  any other pending change.
- `kayden` starts the saved frontend when it is a terminal frontend and the TUI otherwise;
  `kayden-desktop` starts the saved frontend when it is a window frontend and the desktop
  app otherwise.

### Memory changes

A different Memory module is a different archive: conversations and long-term memory of the
old one are not visible after the restart, and come back when the user switches back. The
settings screen says so before saving a Memory change.

### Installing both frontends

The docs say how to install `kayden` with the `desktop` extra (Python 3.13, see
`docs/desktop-app.md`), so both frontends are available from one install.

## Alternatives considered

- **Swapping modules live, while running.** A frontend cannot replace itself, a different
  Memory under an open conversation has none of its messages, and the live Core swap needed
  quiescence checks and rollback. Restart-applied changes need none of that.
- **Keeping the API key and work folder live** as settings "of the running modules". Two
  rules for module settings, and the live Core and Tools rebuild with its quiescence checks
  stays; the criterion above has no exceptions.
- **Typing an import path in the app** (`package.module:factory`). Works for any module
  with no registration, but non-technical users cannot know paths, and a typo is found only
  at the next start.
- **A plugin folder in the data directory** (drop `.py` files into `~/.kayden/plugins`).
  No packaging needed, but Kayden would execute whatever lands in that folder and has no
  dependency handling; entry points make installing the trust decision and resolve
  dependencies.
- **A supervisor process that restarts the instance.** Can restart without the launcher
  and could open a terminal for a terminal frontend, but adds a process and a protocol.
- **Re-executing the launcher (`os.execv`) on restart.** Reloads changed module code, but
  needs the arguments rewritten to reopen the conversation and cannot be tested in-process;
  a loop in the launcher restarts the same way without leaving the process.
- **Stopping startup with a one-line message** when a module cannot be built. Fine for a
  developer, a dead end for a non-technical user who can only fix it in the app.
- **Keeping providers as a separate list beside Core modules.** Two ways to pick a Core
  confuse users and duplicate code; a provider is a Core with preset options.

## Acceptance criteria

- A package that declares a `kayden.frontends` (or other) entry point appears in the
  settings screen of every frontend after installation, with no config editing; one whose
  package does not import is not listed.
- Choosing a different module, key or work folder saves it, shows that it applies after a
  restart, and leaves the running instance and conversation untouched.
- Restart now brings Kayden back with the new modules and the same conversation.
- In the TUI, choosing the desktop frontend and pressing Restart now quits the TUI and opens
  the desktop app on the same data directory and conversation (without a display, the TUI
  says when it will be used). The desktop app choosing the TUI says it will be used the
  next time `kayden` is run.
- A missing key, an uninstalled module id or bad module options open the frontend with
  settings and the error; fixing them and pressing Restart now starts Kayden.
- On first run the key is entered before the Core is built.
- Existing `config.toml` files with import paths keep working.

## Risks

- **Plugins are code.** An installed module runs with the user's permissions; listing only
  installed packages leaves the trust decision to installation, which users may not treat as
  one. The docs say so.
- **Losing live key, work-folder and provider changes.** Each now needs a restart; a few
  seconds for the user, and the conversation continues after it.
- **Memory switches look like lost data.** The warning before saving must be clear.
- **Overlap with other proposals.** The [autonomy note](../implemented/2026-11-07-autonomy-into-sessions.md)
  removes modes and changes Memory's contract, and the
  [desktop app note](2026-11-07-desktop-app.md) adds `kayden-desktop`; this is built on top
  of both.
- **The settings note changes.** Its "frontend is not a setting" and live provider switch
  become wrong once this is built; it is rewritten then.
