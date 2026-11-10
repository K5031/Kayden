# Agent Note: Swappable modules from within the app

Status: implemented — the Core, Tools and Frontend are chosen from the installed ones in the app, applied at the next start; Memory only in config.toml

## Problem

Kayden's architecture makes Core, Memory, Tools and Frontend swappable, and users should be
able to write their own modules, frontends included. A different module meant editing
`[core]`, `[memory]`, `[tools]` or `[frontend]` in `config.toml` with a Python import path,
which a non-technical user cannot do, and Kayden could not list what is installed. The
settings screen offered only a fixed list of providers, and a frontend could not be chosen
at all, since a frontend cannot replace itself while it runs. The owner's main need is to
change the frontend from inside the app: from the TUI to the desktop app and back.

## Decision

### Rule: runtime settings apply live, module settings at the next start

A setting is a **runtime setting** if the runtime itself reads it while running: model,
maintenance model, system prompt, budget, permissions. These apply live. A
setting is a **module setting** if it is only passed into a module's factory or read when
the module is built: which module of each kind, the API key, the Tools' starting `root`,
profiles, prices, context windows, base URLs, compaction options. These are saved
at once and apply at the next start of Kayden; the running instance keeps its modules until
then, so the user keeps working. There are no exceptions: nothing rebuilds a module while
Kayden runs, so module settings save in any state.

The per-model context window is a Core option (its `models` option); the runtime has no
copy of it.

### Discovery: entry points

Installed modules are Python entry points, one group per kind: `kayden.cores`,
`kayden.memories`, `kayden.tools`, `kayden.frontends`. The entry point's name is the
module's id; its target is the factory, `factory(options, paths)`. Kayden's own modules
register the same way in `pyproject.toml`:

| Group | Built-in ids |
|---|---|
| `kayden.cores` | `anthropic`, `deepseek`, `openai` |
| `kayden.memories` | `sqlite` |
| `kayden.tools` | `shell-files` |
| `kayden.frontends` | `tui`, `desktop` |

A provider is a Core entry: the DeepSeek and OpenAI profiles have their own factories
(`kayden.core.openai_compat:deepseek_factory`, `openai_factory`). A factory may carry
`display_name`; a Core factory `key_name` (its key's environment variable and keychain
entry); a frontend factory `kind`, `"terminal"` (the default) or `"window"`. Only entry
points whose package imports are listed (`runtime/modules.py`, `installed()`). The desktop
frontend's package imports PySide6 and qasync at the top, so it is listed only where the
`desktop` extra is installed.

### Settings

- `Settings.modules` holds, per kind, a `ModuleChoice`: the id running now, the id chosen
  for the next start, and the installed modules (`ModuleInfo`: id, name, key name and
  whether a key is set, frontend kind). A module named by import path in `config.toml` has
  no id.
- `Settings.restart_needed` says saved module settings differ from the running instance;
  `restart_frontend` names the frontend Restart now brings up; `startup_error` says why the
  modules could not be built, and `already_open` marks the case where another Kayden holds
  the data folder, so frontends say to close it rather than to fix the settings.
  `work_folder` is the saved one.
- `SettingsUpdate.modules` maps kinds to installed ids (`ValueError` otherwise). Choices
  are saved under `[modules]` in `settings.toml`. A Core change also saves `model = ""`
  and `maintenance_model = ""`, so the new Core starts on its first model; model changes
  made while another Core waits apply live and are not saved.
- `api_key` goes to the keychain for the chosen Core and applies at the next start.
- `ModuleSettings` (`runtime/modules.py`) validates, saves and tracks this part for both
  runtimes.

### Startup

- `kayden` builds the config from `config.toml`, then `settings.toml`, then command-line
  flags. A chosen module keeps `config.toml`'s options only when it is the module named
  there. Import paths in `config.toml` keep working.
- `run_instance` builds the frontend first, then Core, Memory and Tools. When they cannot
  be built (no key for a Core that needs one, a saved id that is not installed, bad
  options), the frontend runs on a `SetupRuntime` (`runtime/setup.py`): `settings()`
  carries `startup_error`, module settings and the runtime settings that need no Core
  (budget, permissions, system prompt) are saved, `restart()` tries again, and every other
  command raises `RuntimeStateError`. A frontend that cannot be built is replaced by the
  launcher's own one, which shows the error.
- First run is that case. The TUI opens its settings with the error and the key field; the
  desktop app shows its welcome. The key is stored before the Core is constructed, and
  Restart now builds it.

### Restart

- `RuntimeAPI.restart()` shuts the instance down as on quit (running tools end as by
  Stop); the event stream ends, so the frontend's `run()` returns. `run_instance` returns
  a `Restart` (frontend id and kind, the open conversation unless the Memory changes), and
  `launch()` in `__main__.py` builds the next instance in the same process, with the
  frontend starting in that conversation.
- `FrontendInfo.kind` says which launcher runs a frontend: `kayden` (also `kd`) runs
  terminal frontends, the window launcher (`kayden.window`) window frontends. Neither knows
  a toolkit: each runs a frontend on the event loop its factory names (`event_loop`,
  default `asyncio.run`; the desktop app names Qt's through qasync,
  `frontends/desktop/loop.py`), and a Restart to a frontend on another loop leaves the
  current one and starts that one (`__main__.run_frontends`). With a saved window frontend
  `kayden` hands over to the window launcher at launch (as for Restart now, below) when a
  display is available, without starting the TUI; it runs the TUI, saying why in one line,
  only without a display or when the window frontend is not installed. The window
  launcher, run directly or by a bundle, opens the desktop app even with a saved terminal
  frontend, since a window cannot open a terminal.
- From a terminal frontend to a window frontend, when a display is available, Restart now
  ends the TUI and `kayden` starts the window launcher (`python -m kayden.window`) as a
  detached process on the same data directory,
  config file and conversation. Without a display, `restart_frontend` stays the running
  one and the TUI says the desktop app will be used the next time Kayden is started from
  the app menu. From a window frontend to a terminal one, the desktop app says the TUI will
  be used the next time `kayden` is run; Restart now restarts the desktop app for any
  other pending change.
- Frontends offer Restart now in their settings and after saving a module setting;
  `kayden.frontends.common.restart_notice` words what waits for it.

### Memory is not offered in the app

A different Memory module is a different archive: conversations and long-term memory of the
old one are not shown after the restart, and come back when the user switches back. That
is a choice of storage, not of how Kayden works, so the settings screens of both frontends
offer the Core, Tools and Frontend only (`kayden.frontends.common.MODULE_LABELS`). Memory
stays a module: it is chosen with `[memory]` in `config.toml`, or by id through
`SettingsUpdate.modules` from any frontend that wants to offer it.

Code: `api.py` (`ModuleInfo`, `ModuleChoice`, `Settings`, `SettingsUpdate`,
`RuntimeAPI.restart`, `FrontendInfo.kind`), `runtime/modules.py`, `runtime/setup.py`,
`runtime/instance.py` (`run_instance`), `__main__.py` (`load_config`, `launch`,
`hand_over`, `run_frontends`), `window.py`, `frontends/desktop/loop.py`, `frontends/common.py`. Tests:
`tests/test_modules.py`, `tests/test_settings.py`, `tests/test_tui_settings.py`,
`tests/test_desktop_window.py`. User docs: `docs/settings.md`, `docs/desktop-app.md`,
`docs/development.md`.

## Alternatives considered

- **Offering Memory in the app too**, with a warning before saving. Built and removed:
  Kayden has one memory module, a switch only hides every conversation, and the warning
  had to explain why. Users who want another archive edit `config.toml`.
- **Swapping modules live, while running.** A frontend cannot replace itself, a different
  Memory under an open conversation has none of its messages, and the live Core swap needed
  quiescence checks and rollback. Restart-applied changes need none of that.
- **Rebuilding the running Core and Tools for a new key or work folder** (keeping them
  live as settings "of the running modules"). Two rules for module settings, and the live
  rebuild with its quiescence checks stays. The folder for new conversations applies live
  without a rebuild: the runtime hands each conversation's folder to the Tools when it
  opens it ([folders per conversation](2026-11-10-folder-per-conversation.md)).
- **Typing an import path in the app** (`package.module:factory`). Works for any module
  with no registration, but non-technical users cannot know paths, and a typo is found only
  at the next start.
- **A plugin folder in the data directory** (drop `.py` files into `~/.kayden/plugins`).
  No packaging needed, but Kayden would execute whatever lands in that folder and has no
  dependency handling; entry points make installing the trust decision and resolve
  dependencies.
- **A supervisor process that restarts the instance.** Could restart without the launcher
  and open a terminal for a terminal frontend, but adds a process and a protocol.
- **Re-executing the launcher (`os.execv`) on restart.** Reloads changed module code, but
  needs the arguments rewritten to reopen the conversation and cannot be tested in-process;
  a loop in the launcher restarts the same way without leaving the process.
- **Stopping startup with a one-line message** when a module cannot be built. Fine for a
  developer, a dead end for a non-technical user who can only fix it in the app.
- **Keeping providers as a separate list beside Core modules.** Two ways to pick a Core
  confuse users and duplicate code; a provider is a Core with preset options.

## Consequences

- Any installed package can add a provider, Memory, Tools or interface that every frontend
  lists, with no config editing. Plugins are code: an installed module runs with the user's
  permissions, and listing only installed packages leaves the trust decision to
  installation, which users may not treat as one. The docs say so.
- Changing the provider or key needs a restart: a few seconds, and the
  conversation continues after it. A key is checked by the provider only after the
  restart, so the desktop welcome no longer rejects a bad key on the spot; the error shows
  when the conversation starts.
- The runtime lost the provider list, module rebuilding and quiescence checks, and gained
  a setup runtime and module discovery. `settings()` imports every installed module once
  per process, PySide6 included where the desktop extra is installed.
- Restarting in the same process keeps module code loaded at the first start; code
  changed on disk needs Kayden to be started again.
- Choosing Memory takes editing `config.toml`; nobody hides their conversations by
  picking from a list.
- Choosing the desktop app means it opens however Kayden is started where a display is,
  and a saved frontend never blocks a launch: each launcher falls back to its own.
- The [settings note](2026-11-07-settings-for-non-technical-users.md) describes the
  settings in these terms.
