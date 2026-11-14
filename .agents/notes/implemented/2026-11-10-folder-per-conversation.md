# Agent Note: A folder per conversation

Status: implemented — each conversation has its own work folder, chosen when it starts

## Problem

Kadence had one work folder for the whole app: the Tools' `root`, a module setting that
applied at the next start. Work is organised by project, so a user who wanted the agent in
another project changed Settings, restarted, and every conversation then pointed at that
folder, the old ones included. A conversation about one project should stay with that
project's folder, and starting work somewhere else should be one click, not a restart.

## Decision

### Conversations keep their folder

- `ConversationInfo.folder` is the conversation's work folder, an absolute path; None
  means the Tools' default and occurs only with Tools that have a fixed folder.
  `Memory.create_conversation(..., folder=)` stores it. SqliteMemory has a `folder`
  column.
- `StartOptions.folder` picks the folder of a new conversation (ValueError with
  `conversation_id`, or when it is not a folder). Without it the new conversation gets the
  `seed_from` conversation's folder, else `Settings.work_folder`, stored as a path, so
  changing the default later moves no conversation.

### Tools follow the open conversation

- `Tools.use_folder(folder: Path | None)` is optional. The runtime calls it in `start()`,
  before any call of that conversation and with no operation running (start already
  requires that). None means the module's default. The base class raises
  NotImplementedError: such a module has one fixed folder, and `StartOptions.folder` is
  refused.
- `ShellFilesTools.use_folder` sets the root, the boundary check and the sandbox for that
  folder, creating it when missing (a folder deleted since, or a new default). If it
  cannot, `start()` raises ValueError naming the folder and creates nothing.
- The bubblewrap command is built per folder (`_sandbox`); whether bubblewrap works is
  tested once at construction (`_bwrap`).

### The default folder applies live

`Settings.work_folder` is the folder new conversations get. The runtime hands the folder
over at every `start()`, so changing it needs no restart: it applies to the next new
conversation and does not set `restart_needed`. The terminal app's settings call it
"Folder for new chats".

### Frontends

- **Desktop app.** A new chat shows a folder button: recent folders and a folder picker,
  and a chat starts in the one chosen (`Controller.new_in_folder`); an unused chat left
  behind was never stored (`.agents/decisions.md`, New chats). The chat is not used before
  a folder is chosen for it
  ([a desktop chat is given its folder](2026-11-14-a-desktop-chat-is-given-its-folder.md)).
- **TUI.** The start screen has a folder field (empty: the default); `/folder` shows the
  folder and `/folder PATH` behaves like the desktop button; the status line shows the
  folder.
- `frontends.common.folder_path` shows paths with `~` for the home folder.

## Alternatives considered

- **Passing the folder with every call** (`ToolContext.work_folder`, `confined(call,
  folder)`). Stateless, but two signatures change for a value that is fixed for a
  conversation's whole life, and one agent per process already means one folder at a time.
- **Changing the folder of a running conversation.** Earlier tool results, paths and the
  agent's plan refer to the old folder; a new conversation in the other folder, or
  "Continue from" with a folder, is clearer.
- **Asking for a folder on every New chat, in every frontend.** The terminal app is
  started in a folder, which is the answer there. The desktop app does ask
  ([a desktop chat is given its folder](2026-11-14-a-desktop-chat-is-given-its-folder.md)).
- **Keeping the default folder a module setting with a restart.** Consistent with the
  other Tools options, but a restart for something the runtime can apply at the next
  `start()` is the friction this note removes.

## Consequences

- Switching projects is one click or one command, and an old conversation reopens where it
  worked, after a restart or a change of the default.
- `Tools` gained an optional method, and `start()` can now fail for a folder that cannot
  be created.
- **Schema 6 refuses a schema 5 store.** The user's store is converted once with an ALTER;
  Kadence carries no migration code.
- **A deleted folder is recreated empty** when its conversation reopens; the agent may be
  confused that its files are gone. The folder is shown, so the user can see why.
