# Agent Note: A desktop chat is given its folder

Status: implemented — a new chat in the desktop app works nowhere until the user chooses its folder

Changes the desktop part of [a folder per conversation](2026-11-10-folder-per-conversation.md).

## Problem

Started from the app menu, the desktop app has no meaningful current folder. It fell back
to `~/Kadence`, which it created, so every chat the user had not placed worked there:
files of unrelated tasks piled up in one folder the user never chose, and the project a
chat belonged to was a guess. The user compared it with Claude's desktop app, where a
session is always started on a folder the user picks, and asked for the same.

## Decision

- **No folder of the app's own.** `kadence-desktop` starts its Tools in the current
  directory, as `kadence` does. `~/Kadence` and `run_frontends(default_root=)` are gone.
- **A new chat asks.** `Controller.folder_picked` is true for a chat opened with a
  folder, a conversation id or `seed_from`, and false for a plain new chat. While it is
  false and the chat is unused, the folder button reads "Choose folder", the title has
  no folder tooltip, and sending a message or opening the duty dialog opens the folder
  menu instead (`MainWindow._offer_folders`); what was typed stays in the box.
- **Choosing** is the existing menu: recent folders and "Choose folder…". The folder the
  draft happens to have can be chosen too.
- **Nothing to set beforehand.** The desktop Settings and the welcome have no folder
  field. `Settings.work_folder` remains the runtime's default for a new conversation and
  the terminal app still shows and edits it.
- Tools with one fixed folder (`ConversationInfo.folder` None) are not asked about.

## Alternatives considered

- **Start a new chat in the folder used last.** One click less, and what the note linked
  above chose in spirit. The user was offered it and chose asking every time: the folder
  is then always a decision, never an inheritance.
- **Keep `~/Kadence` as the fallback.** The problem above.
- **Fall back to the home folder.** Nothing in the home folder counts as inside the work
  folder ([free in the work folder](2026-11-10-free-in-the-work-folder.md)), so every
  action would ask.

## Consequences

- Every new chat costs one click before the first message.
- No folder appears in the user's home that they did not make.
- The desktop app and the terminal app differ: the terminal app works where it is
  started and needs no choice.
