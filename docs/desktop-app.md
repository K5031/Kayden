# Desktop app

The desktop app is a window for Kayden: a conversation list on the left, the conversation
in the middle, and a message box at the bottom. It does everything the terminal app does
in Session mode, without a terminal.

## Install

Ready-made downloads (a `.dmg` for macOS, a Flatpak and an AppImage for Linux) are not
published yet. Until then, run the app from a checkout (see
[Getting started](getting-started.md) for cloning):

```bash
cd Kayden
uv run --python 3.13 --extra desktop kayden-desktop
```

The app is tested on Python 3.13. The Qt libraries it uses are not fully compatible with
Python 3.15 yet.

## First start

When no API key is saved, a welcome window asks for three things:

1. **Your AI provider**: Claude, DeepSeek or OpenAI. **Get a key** opens the provider's
   website.
2. **Your API key.** Kayden checks it with the provider and keeps it in your computer's
   keychain. If the key is refused, nothing changes and you can try again. If your computer
   has no keychain, set the environment variable named in the message instead
   (`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY` or `OPENAI_API_KEY`).
3. **A work folder and a spending limit.** The work folder is where Kayden runs commands
   and reads and writes files; it is `~/Kayden` unless you choose another. The spending
   limit is optional.

## Using it

| To | Do this |
|---|---|
| Send a message | Type it and press **Enter**. **Shift+Enter** starts a new line. |
| Stop Kayden | Press **Stop** (the Send button while Kayden works) or **Esc**. |
| Answer a permission request | Choose **Allow once**, **Allow for this conversation** or **Deny** on the card above the message box. |
| Cancel one task | Press **Cancel** on its card. |
| See what a tool did | Press **Details** on its card. |
| See the model's reasoning | Open **Thinking** above a reply. |
| Start over | **New conversation** in the list. |
| Go back to a conversation | Click it in the list. |
| Change the model | The **Model** menu. |
| Change settings | **Kayden → Settings** (**Ctrl+,**). |

Messages you send while Kayden works are queued and shown greyed until Kayden reads them.
You can switch conversations only while Kayden is not working on anything.

The status bar shows what Kayden is doing, how many tasks run in the background, the model
and how much has been spent.

At start the app reopens your most recent conversation.

## Settings

The Settings window has the same settings as the terminal app; see [Settings](settings.md).
Raising or removing the spending limit, or letting Kayden do more without asking, asks you
to confirm first.

## Good to know

- The app and the terminal app share the same data folder (`~/.kayden`). Only one of them
  can be open at a time; the other says Kayden is already open.
- Closing the window stops running tasks, as quitting the terminal app does.
- The app does not offer Continuous mode.
