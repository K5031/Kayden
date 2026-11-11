# Agent Note: Desktop app

Status: proposed — a PySide6 (Qt Widgets) frontend on a qasync event loop, packaged with Briefcase

## Problem

Kayden is meant for people who never open a terminal, and the owner chose a desktop app as
the first graphical frontend, before any web frontend
([settings note](../implemented/2026-11-07-settings-for-non-technical-users.md)). Today
the only frontend is the Textual TUI. Using Kayden means installing Python and uv, running
`kayden` from a shell in the folder to work in, and reading a terminal transcript. Settings
already live in the runtime (`RuntimeAPI.settings()` / `update_settings()`), so a frontend
can offer them; what is missing is an app a person can download, open by double-clicking,
set up by pasting an API key, and chat with.

The app has to meet the runtime's rules. The runtime runs on asyncio and must never be
blocked. Late-attach recovery follows the `RuntimeSnapshot` order. Loosening permissions
and raising or removing the spending limit need the user's confirmation first.

## Proposal

### Toolkit: PySide6 Qt Widgets on a qasync loop

The app is built on PySide6 (Qt for Python, LGPL-3.0) using Qt Widgets. QtWebEngine is not
used. The asyncio loop is qasync's `QEventLoop`, so Qt and the runtime share one loop on the
main thread.

- **One loop, no threads.** `run_instance` is a coroutine, and `Frontend.run` is awaited
  inside it. qasync runs a full asyncio loop on Qt's event loop: `asyncio.run(coro,
  loop_factory=qasync.QEventLoop)`. Runtime tasks, Core streams, `asyncio.to_thread`
  module loading and the shell tool's subprocesses all run as they do under the TUI. Qt
  signals call into coroutines with `asyncio.create_task`.
- **Why qasync and not QtAsyncio.** QtAsyncio, Qt's own integration, is still a technical
  preview. It has no transports, sockets, servers or subprocesses, so the Core's HTTP
  client and the shell tool cannot run on it.
- **Rich chat text without a web engine.** `QTextBrowser.setMarkdown` renders Markdown,
  including tables and code blocks. Leaving out QtWebEngine keeps the bundle to Qt's
  Essentials modules.
- **Maturity and maintenance.** PySide6 6.12.0 was released 2026-10-08 by The Qt Company,
  with releases every few weeks and Python 3.10 to 3.15 supported. Qt widgets look native
  on Windows and macOS and adopt the platform style on Linux. They expose UI Automation,
  NSAccessibility and AT-SPI to screen readers.
- **Licensing.** PySide6 is LGPL-3.0, which is compatible with Kayden's MIT license when Qt
  is linked dynamically. Bundles ship Qt as separate shared libraries plus its license
  notices, never as one static file. qasync is BSD-2-Clause.

### Launching: a desktop entry point

The `kayden` launcher calls `asyncio.run` and only then loads the frontend, so a frontend
module cannot choose the event loop. That is wrong for any GUI toolkit, whose loop must be
the one that runs. The desktop app therefore gets its own launcher,
`kayden.window`, the window launcher (what the bundle runs; `kayden` hands over to it). It does the following:

1. Reads `config.toml` and `settings.toml` with the same `load_config` as `kayden`.
2. Uses the desktop frontend factory instead of the configured frontend.
3. Sets the Tools root to `~/Kayden` when neither file sets a work folder
   (`load_config(default_root=...)`). A double-clicked app's working directory is
   meaningless, often `/`, and the root must never be that.
4. Runs `asyncio.run(run_instance(config), loop_factory=qasync.QEventLoop)` under a
   `QApplication` that does not quit when its last window closes, so the runtime shuts down
   before the loop stops.

A Briefcase bundle runs `python -m <app module>`, so a two-line `kayden_desktop` package
calls the same `main`.

"Frontend owns the process's main loop" (`api.Frontend` docstring) does not match how the
loop is created. That mismatch goes into `.agents/contract-issues.md` when this is built.

### Talking to Kayden

The app is an ordinary `Frontend` module (`kayden.frontends.desktop`). Its
`FrontendInfo` has `interactive_permissions=True`, and `run(runtime)` returns when the main
window closes.

It is split into two layers:

- **A view model with no toolkit.** It turns `EventEnvelope`s into conversation state:
  entries, the stream in progress, tool cards, prompts, status. It also holds the recovery
  logic and the settings diff. It is tested without a display, against the runtime with
  `ScriptedCore` and `FakeTools`, and it is the part a web frontend could reuse.
- **Qt widgets.** They render that state.

Event plumbing works as in the TUI. A pump task copies `runtime.events()` into an inbox, and
one consumer task applies envelopes in `seq` order. Runtime commands run as separate tasks,
so a slow `stop()` or `update_settings()` never freezes the window. Their
`RuntimeStateError` and `ValueError` messages appear as inline notices, never as a modal
`exec()`. Dialogs use `open()` and signals, because `exec()` would start a nested event loop
inside the runtime's loop.

**Rendering the event stream:**

| Events | What the user sees |
|---|---|
| `TextDelta`, `ReasoningDelta` | The assistant bubble grows. Markdown is re-rendered at most every ~100 ms. Reasoning goes in a collapsed "Thinking" section. |
| `ModelRetry` | The partial bubble is cleared, and "Retrying (2 of 5)…" appears in the status line. |
| `MessageQueued`, `MessageCommitted` | A queued message shows dimmed until a commit with the same `message_id` arrives. A commit replaces the streamed bubble with the final text. `stop` marks interrupted or refused replies. |
| `ToolCallDelta`, `ToolStarted`, `ToolProgress`, `ToolBackgrounded`, `ToolFinished` | A tool card shows the tool name, a short summary of the arguments, its state (preparing, running, in the background, done, failed, declined, cancelled), the last progress line, and the duration. A running card has a Cancel button (`cancel_operation`). Details expand on click. |
| `PermissionRequested`, `PermissionResolved` | An inline card says what the agent wants to do and whether it only reads, with **Allow once**, **Allow for this conversation** and **Deny**. Clicks are first-wins, and the card is removed on `PermissionResolved`. Runtime-side outcomes are noted on the tool card. |
| `StateChanged` | Status line: Ready, Thinking, Working, Waiting for you, Stopping, Stopped. It also shows "*n* tasks running in the background" from `pending_ops`. The Send button becomes Stop while a cycle runs. Messages typed meanwhile are queued. |
| `UsageUpdated`, `ModelSelected` | Status line: model name and "$1.20 of $10 spent". |
| `MaintenanceStarted`, `MaintenanceFinished`, `StopStalled` | Status notices. Abandoned calls also get a warning in the transcript. |
| `Failure` | A banner. A budget failure points to Settings. |
| `ShuttingDown` | A final notice, then the window closes. |

**Recovery.** Recovery runs at startup and on any `seq` gap, since subscriber queues drop
the oldest envelopes. It follows the `RuntimeSnapshot` order:

1. `events()` is already subscribed.
2. Take `snapshot()`, and drop envelopes with `seq <= snapshot.seq`.
3. Clear the view, then load `history(limit=100)`, de-duplicating by `Message.id`.
4. Rebuild tool cards from `snapshot.operations`, including `backgrounded` and
   `last_progress`.
5. Rebuild queued messages and pending permission cards.

"Load earlier messages" pages back with `history(before=...)`.

**Conversations.** The app has no mode choice: there is one kind of conversation
([Autonomy into sessions](../implemented/2026-11-07-autonomy-into-sessions.md)). A sidebar lists
`list_conversations()`, newest first, with a New conversation button. An entry
shows its title, else the start of its first message, else the date. Choosing one calls `start()`, which is
allowed only while idle with no outstanding operations. Otherwise the sidebar is disabled
with the reason shown. A Model menu lists `list_models()` with prices and calls
`select_model`. Search over the archive (`search_archive`) is in a later version.

The conversation controls of the autonomy note (rename, archive,
Continue from, the memory window) are added to the sidebar and Settings as each
lands in the runtime. v1 does not wait for them, and none of them needs a mode.

**Settings window.** The window offers the same fields as the TUI's `SettingsScreen`:

- provider, showing whether a key is saved
- API key, as a password field, empty to keep the current key
- model and maintenance model, with prices
- spending limit, with the amount spent so far from `snapshot().usage`
- permissions: "Ask me first", "Read-only freely", "Nothing"
- instructions
- work folder, chosen with the native folder picker and disabled when `work_folder` is
  None

Saving sends a `SettingsUpdate` with only the changed fields. Switching provider disables
the model fields until saved, as in the TUI. Before sending, the app checks whether the
change loosens anything:

- a permission later in `PermissionPolicy` order than the current one
- a higher spending limit
- removing a spending limit

If it does, a confirmation dialog names each loosening ("This lets the agent do more without
asking: read-only freely. Continue?"). Nothing is sent unless the user confirms.

That check is pure, and the TUI already implements it as `SettingsScreen._loosening`. It
moves to a toolkit-free function used by both frontends, so the safety rule has one
implementation. A provider, key or work-folder change while Kayden is busy shows the
runtime's `RuntimeStateError` as "Finish or stop the current task first". `SettingsChanged`
refreshes the open window and the status line.

### Installation and first run

**Bundles.** Briefcase builds per-OS bundles that contain their own Python, so the user
installs nothing else. Briefcase supports PySide6 apps and produces the formats below.

| OS | Download | Notes |
|---|---|---|
| macOS | `.dmg` with a signed, notarized `.app` | Needs an Apple Developer ID. |
| Linux | Flatpak (main) and AppImage | The Flatpak needs D-Bus access to `org.freedesktop.secrets` for the keychain. |
| Windows | `.msi` | Deferred, see Scope. |

The bundled Python is chosen at build time and is 3.13. On 3.15, PySide6 breaks lazy
imports and qasync needs the fixes in `QtEventLoop` (`.agents/decisions.md`, Desktop app).

**First run.** First run happens when `settings()` shows no provider with `has_key`. It is a
three-page welcome window, shown before `start()`:

1. **Choose your AI provider.** Each entry in `Settings.providers` is shown with a "Get a
   key" link to that provider's console.
2. **Paste your API key.** The app calls `update_settings(SettingsUpdate(provider=…,
   api_key=…))`, which stores the key in the OS keychain and checks it by listing models. A
   rejected key shows the runtime's message, and the old provider stays in place. When the
   machine has no keychain, the runtime refuses the key. The page then explains how to set
   the environment variable instead and links to the docs.
3. **Pick a work folder and a spending limit.** The folder picker defaults to `~/Kayden`,
   and the app sends `work_folder`. An optional spending limit is suggested empty. Setting a
   limit where there was none tightens, so it needs no confirmation. Permissions stay at
   the default, "Ask me first", and the page says where to change them.

Then the app calls `start(StartOptions(mode=Mode.SESSION))` and focuses the message box.

### Look

The app draws its own look instead of the platform's: Qt's Fusion style, a palette and a
style sheet built from it (`kayden.frontends.desktop.theme`), and a few custom-painted
widgets (the switches, sidebar and memory rows, the send and lock icons). There is one
indigo accent; everything else is neutral gray. Light and dark follow the system's color
scheme and switch while the app runs. Your messages are accent bubbles on the right;
Kayden's replies are plain text beside an avatar, which stays readable when long. The
message box is rounded with the send button inside it, and the status line sits under it
instead of in a status bar. The view model stays free of Qt; only `window.py` and `theme.py`
know about the look.

### Scope

**Version 1:**

- Linux and macOS.
- Session Mode only, with no mode choice: the conversation sidebar, new and resume
  conversations.
- Streaming transcript with Markdown, reasoning, tool cards, per-call cancel, permission
  cards, queued messages, Stop, the status line with spending, and the model menu.
- Settings window with confirmations, first-run welcome, and late-attach recovery.
- Briefcase bundles: macOS `.dmg` and Linux Flatpak and AppImage.
- A `docs/` page for installing and using the app.

**Deferred:**

- **Windows.** The runtime's instance lock uses `fcntl`, and the shell tool uses `killpg`
  and a POSIX shell. Porting both needs its own note. The toolkit and Briefcase already
  target Windows.
- Archive search, memory inspection, a tool list, editing the other budget dimensions.
- Automatic updates.
- Moving the TUI onto the shared view model beyond the loosening check.
- A web frontend.

## Alternatives considered

- **Toga (BeeWare).** Toga 0.5.7 has native widgets, an asyncio loop on the main thread and
  async handlers, so it fits Kayden's runtime best. It lost on three points:
  - It is pre-1.0.
  - It has no rich-text or Markdown widget, so the transcript would be HTML in a
    `WebView`, which needs WebKitGTK on Linux.
  - Its widget set is thin for tool cards and inline prompts.

  Briefcase, its packager, is used here anyway.
- **pywebview with an HTML/JS UI.** HTML/JS gives high web reuse with system web views and a
  BSD license. But `webview.start()` must own the main thread, so the runtime would run in a
  second thread and every event and command would cross threads through a JS bridge. Linux
  needs either PyGObject plus WebKit2GTK from system packages or QtWebEngine.
- **NiceGUI native mode.** Version 4.0.0 is asyncio-native, and the same code becomes a web
  app, the most reuse of any option. It loses because it serves the UI over a local HTTP
  port, so any local process could drive the runtime: answer prompts, loosen permissions
  (SPEC 3 warns about malicious frontends). It also opens pywebview in a separate process,
  with pywebview's Linux dependencies. Version 4.0 shipped on 2026-11-04 with breaking
  changes.
- **Flet.** Flet is async-native in 1.x, has a web mode and is Apache-2.0. It loses for
  three reasons:
  - Its look is Material or Cupertino, not native.
  - `flet build` needs the Flutter SDK.
  - 1.0 shipped 2026-09-14 and removed every deprecated 0.x API, so the API is still
    settling.
- **QtAsyncio instead of qasync.** QtAsyncio is first-party, but as a technical preview it
  lacks subprocesses, sockets and transports.
- **PySide6 with QtWebEngine for the transcript.** HTML rendering would be reusable on the
  web, but QtWebEngine bundles Chromium, which multiplies the bundle size.
  `QTextBrowser` Markdown covers what the TUI shows.
- **The platform's own widget style.** Plain Qt widgets need no styling code and match the
  system, but the owner found them gray and dated for a chat app aimed at non-technical
  users, and they look different on every platform.
- **QML (Qt Quick) for a chat look.** It makes custom chat visuals easier, but it is a
  second UI technology next to Widgets and would mean rewriting every window. A style sheet
  reaches the approved design with small widget changes.
- **PyQt6.** It has the same Qt and the same loop story, but a GPL or commercial license.
- **A React UI in Electron or Tauri, with Kayden as a Python sidecar.** It gives the most
  polished UI for the least effort, the largest ecosystem (chat, Markdown, code
  highlighting) and the same UI for a later web frontend. It loses on architecture: the UI
  cannot call `RuntimeAPI` directly, so the runtime must be exposed over a local connection
  that has to be secured against other local processes (the NiceGUI problem), the app
  becomes two processes and two languages, and each OS bundle carries two runtimes
  (Chromium or a system web view, plus Python). The owner chose the simpler single-process
  app over web reuse.
- **Tauri or PyTauri.** Tauri has a tiny shell on system web views. But PyTauri has not
  released since 0.8.0 (2025-09-01), it adds Rust and Node tooling, and bundling Python
  removes the size advantage.
- **Kivy, Dear PyGui.** Their GPU-drawn, non-native UIs with weak screen-reader support do
  not suit non-technical users. Kivy has no Python 3.14 wheels.
- **Runtime in a worker thread under any toolkit.** It would work with every toolkit, but
  every runtime call and event would cross threads. Toga and qasync avoid that.
- **Packaging with PyInstaller or `pyside6-deploy` (Nuitka).** Both freeze the app but do
  not build signed installers, Flatpaks or notarized DMGs. Nuitka's PyPI metadata now
  reads AGPL-3.0, which would need checking.

## Acceptance criteria

- On a clean macOS and a clean Fedora or Ubuntu machine without Python, a user installs from
  the `.dmg` or Flatpak, completes the welcome, and gets a streamed answer without a
  terminal.
- The key ends up in the OS keychain and in no file or log. A rejected key leaves the
  previous provider working.
- View-model tests, written first, cover:
  - every event row above
  - `ModelRetry` discarding the stream
  - recovery on a `seq` gap and on attach mid-cycle, with queued messages, pending
    prompts and background operations
  - the settings diff
- A test shows that loosening permissions, raising the limit and removing the limit each
  require confirmation, and that nothing is sent on No. The TUI and the desktop app use the
  same check.
- The window stays responsive while a tool runs, during Stop with a call that ignores
  cancellation, and while `update_settings` rebuilds the Core.
- The existing runtime and shell-tool tests pass on a `qasync.QEventLoop`, which shows that
  subprocesses and HTTP work on it.
- The bundle size of each format is measured and recorded in `.agents/decisions.md`.
- VoiceOver and Orca can read the transcript and operate Send, Stop and the permission
  buttons.

## Risks

- **qasync has had no release since 0.28.0 (2025-08-28),** and its metadata does not yet
  claim Python 3.14. On 3.13 the runtime, shell-tool and Core suites pass on it file by
  file; on 3.15 one command test hangs and the OpenAI-compatible Core tests crash.
  Mitigations: bundle Python 3.13, vendor the module if it is abandoned (BSD-2-Clause), or
  switch to QtAsyncio once it supports subprocesses and sockets.
- **PySide6 on Python 3.15 breaks lazy imports** (PEP 810) read across modules. The app
  resolves the one asyncio needs; others in the stdlib stay exposed, so 3.15 is not
  supported until PySide6 fixes it.
- **One thread for GUI and runtime.** A slow paint, such as re-rendering a very long
  Markdown reply, delays runtime tasks. The mitigations are throttled re-rendering and
  paged history.
- **Keychain gaps.** A Linux desktop may lack a Secret Service, or a Flatpak may be denied
  access to it. Such users must set an environment variable, which non-technical users
  cannot do.
- **Signing costs and gatekeeping.** Apple notarization needs a paid developer account.
  Unsigned builds are blocked by Gatekeeper.
- **Not the platform's look.** The app looks the same on macOS, GNOME and KDE, and matches
  none of them exactly. System font size and light or dark are followed; the platform's
  accent color, widget shapes and high-contrast themes are not.
- **Web reuse is limited to the view model.** A web frontend will need its own rendering
  layer.
- **Ignoring the configured frontend.** The desktop launcher ignores `[frontend]` in
  `config.toml`, so the same data directory can be opened by either the TUI or the app. The
  instance lock refuses a second agent in the same mode. The app must show that refusal as
  "Kayden is already open" rather than a traceback.

## Sources (checked 2026-11-07)

- PyPI JSON API for versions, dates and licenses: PySide6, qasync, toga, briefcase,
  pywebview, flet, nicegui, pyinstaller, nuitka.
- QtAsyncio status: https://doc.qt.io/qtforpython-6/PySide6/QtAsyncio/index.html
- qasync: https://github.com/CabbageDevelopment/qasync
- Toga event loop: https://toga.beeware.org/en/stable/reference/api/application/app/
- Toga widgets: https://toga.beeware.org/en/stable/reference/api/index.html
- Briefcase platforms: https://briefcase.beeware.org/en/stable/reference/platforms/
- pywebview threading: https://pywebview.flowrl.com/guide/usage.html
- pywebview architecture: https://pywebview.flowrl.com/guide/architecture.html
- NiceGUI 4.0: https://github.com/zauberzeug/nicegui/releases/tag/v4.0.0
- NiceGUI deployment:
  https://nicegui.io/documentation/section_configuration_deployment
- Flet 1.0: https://flet.dev/blog/flet-1-0
- Flet breaking changes:
  https://flet.dev/docs/updates/breaking-changes/v1-0-0/removed-deprecated-apis
- PyTauri: https://github.com/pytauri/pytauri

The bundle sizes, Briefcase's MSI, DMG and notarization output, and qasync's subprocess
support on 3.14 have not been checked first-hand. The acceptance criteria measure them.
