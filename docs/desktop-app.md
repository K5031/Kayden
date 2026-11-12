# Desktop app

The desktop app is a window for Kayden: a conversation list on the left, the conversation
in the middle, and a message box at the bottom. It does what the terminal app does, without
a terminal.

![The desktop app, light](images/main-light.png)

It follows your computer's light or dark setting, and switches when you change it. To
keep it light or dark whatever the computer is set to, choose **☰ → View → Theme**; the
choice is remembered.

![The desktop app, dark](images/main-dark.png)

## Install

Ready-made downloads (a `.dmg` for macOS, a Flatpak and an AppImage for Linux) are not
published yet. Until then, install Kayden with its `desktop` extra from a checkout (see
[Getting started](getting-started.md) for cloning):

```bash
uv tool install --python 3.13 --editable "Kayden[desktop]"
```

Then start it:

```bash
kayden-desktop
```

`kayden` still starts the terminal app; both show the same conversations. To run the
desktop app without installing:

```bash
cd Kayden
uv run --python 3.13 --extra desktop kayden-desktop
```

On Linux, choose your name at the bottom left → **Add to App Menu** once the app is open. Kayden then appears in
your app menu with its own icon and starts without a terminal. The menu item is there only
while the app menu has no entry for this install.

The app is tested on Python 3.13. The Qt libraries it uses are not fully compatible with
Python 3.15 yet. Without the `desktop` extra, `kayden-desktop` cannot start.

## First start

When no API key is saved, a welcome window asks for three things:

1. **Your AI provider**: Claude, DeepSeek or OpenAI. **Get a key** opens the provider's
   website.
2. **Your API key.** Kayden keeps it in your computer's keychain. If your computer has no
   keychain, set the environment variable named in the message instead
   (`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY` or `OPENAI_API_KEY`).
3. **A work folder and a spending limit.** The work folder is where Kayden runs commands
   and reads and writes files; it is `~/Kayden` unless you choose another. The spending
   limit is optional.

Kayden then restarts with your provider. If the provider refuses the key, the message says
so; enter another one in Settings and choose **Restart now**. If Kayden cannot start for
another reason, the Settings window opens with the message.

## Using it

| To | Do this |
|---|---|
| Move, resize or maximize the window | Drag the conversation's title bar or the top of the sidebar; double-click it to maximize. Drag any edge to resize. Minimize, maximize and close are at the top right. |
| Find the menus | **☰** at the top of the sidebar: new chat, duty, stop, view (zoom, theme, archived conversations) and model. Your name at the bottom left: settings, memory, tool servers, help and quit; the gear next to it opens Settings. Shortcuts work without opening a menu. |
| Send a message | Type it and press **Enter**. **Shift+Enter** starts a new line. |
| Attach a file | Click **+** in the message box, drop it anywhere on the window, or paste it (a copied picture or screenshot works too). It waits above the message box, a picture with a small preview, until you send; ✕ takes it off. See [Attaching files](using-kayden.md#attaching-files). |
| Run a skill | Send its name as a message: `/consolidate-memory`. See [Skills](concepts.md#skills). |
| Stop Kayden | Press the round button in the message box (it shows a square while Kayden works) or **Esc**. |
| Answer a permission request | Choose **Allow once**, **Allow for this conversation** or **Deny** on the card above the message box. |
| Answer with a click | When Kayden needs a decision, a card above the message box asks the question and offers buttons, Kayden's pick first. A click sends that answer, or does what it says. ✕ closes the card. See [The wrap-up card](using-kayden.md#the-wrap-up-card). |
| Answer a proposal | When Kayden proposes something of its own, a card above the message box says what and why: **Approve** runs it, **Dismiss** declines. See [Proposals](using-kayden.md#proposals). |
| Cancel one task | Press **Cancel** on its card. |
| See what a tool did | Steps between two replies fold into one line: the step under way while Kayden works ("Search the web for flights to Oslo"), then how many steps there were and any that failed ("4 steps · 1 failed"). Click it to see each step and the thinking between them; point at a step to see the tool and its exact input, and press its arrow for the result. A step without a sentence shows the tool and its input. |
| See the model's reasoning | **Thinking…** shows under the conversation as soon as Kayden starts on its next step. Once the reasoning arrives it shows above the reply as **Thinking…**, then **Thought for 4s** when it is over: click it to read it. |
| Copy a reply | Point at it: a copy button and how long ago it came appear under it. |
| Start over | **New chat** (**Ctrl+N**). Nothing is added to the list until you send your first message. |
| Work in another folder | In a new chat, the folder button in the middle: pick a recent one or **Choose folder…**. Any time: **☰ → New chat in another folder…**. A new chat starts in it. See [Folders](using-kayden.md#folders). |
| Go back to a conversation | Click it in the list. |
| Rename, archive or delete one | Right-click it (or select it and press the Menu key): **Rename…** (**F2**), **Archive**, **Delete…** (**Delete**). Turn on **☰ → View → Show archived conversations** to see hidden ones. |
| Pick up where one left off | Right-click it: **Continue in a new conversation**. |
| See what Kayden remembers | **Settings → Memory** (or your name at the bottom left → **Memory…**): About you and the notes it keeps across conversations; add, edit or delete them. |
| Give Kayden more tools | **Settings → Tool servers**: add GitHub, a database and the like. See [More tools](using-kayden.md#more-tools-mcp). |
| Open work Kayden handed off | Its sub-sessions are listed under the conversation, with a dot while they work: click one to open it there, talk to it, approve its steps or stop it. Click the conversation to go back. Right-click one to resume, change its spending limit, rename, archive or delete it; the limit for new ones is in **Settings**. See [Sub-sessions](concepts.md#sub-sessions). |
| Fold a project | Conversations are grouped by their folder; click a folder's name to fold or unfold it. |
| Give a conversation standing work | **☰ → Duty…**: what to keep doing, for how long, how often to check in, and the plan; **Turn off** ends it. See [Duties](concepts.md#duties). |
| Change the model | Click the model's name under the message box, or **☰ → Model**. |
| Change what Kayden may do on its own | Click the mode under the message box (Auto, Work folder, Read only, Ask first, No tools). Letting it do more asks you first. See [Permissions](settings.md#permissions). |
| Change settings | The gear at the bottom left, or **Ctrl+,**. Settings has sections down the left: General, Spending, Permissions, Instructions, Memory and Tool servers. |
| Make everything larger or smaller | **Ctrl +** and **Ctrl −** (also in **☰ → View**); **Ctrl+0** goes back to normal. Kayden remembers the size. |

Each conversation in the list shows its name, when it was last used, and the start of its
first message. Until Kayden names a conversation, the start of its first message is its
name. A new chat joins the list with your first message; if you leave it before writing
anything, it is not kept.

Messages you send while Kayden works are queued and shown greyed until Kayden reads them.
You can switch conversations only while Kayden is not working on anything; renaming,
archiving and deleting work any time.

**Delete…** asks first: deleting erases the conversation and its summary, and cannot be
undone. What Kayden remembered from it stays in Memory, where you can forget it too. To only hide a conversation, choose **Archive**. Deleting the open
conversation opens a new chat in its place, once Kayden is not working. See [Deleting](concepts.md#deleting) for what
deletion does not reach.

Hover the conversation's title to see its folder. What Kayden has spent, across all
conversations, is at the top of the menu under your name; when you pass 80% of your
spending limit, a notice in the conversation says so. Under the message box are the permission mode and the model. A conversation with a duty shows when it checks in next, next to its title.

At start the app opens maximized and reopens your most recent conversation, or the one you were in when you
switched from the terminal app.

## Settings

The Settings window has the same settings as the terminal app; see [Settings](settings.md).
Raising or removing the spending limit, or letting Kayden do more without asking, asks you
to confirm first. The provider, key and modules apply when Kayden restarts: **Restart now**
in the Settings window does that. **Folder for new chats** applies to the next new chat.

## Switching to the terminal app

Run `kayden` in a terminal. It opens the same conversations with the same settings; close
this app first, since one Kayden uses a data folder at a time.

## Good to know

- The app and the terminal app share the same data folder (`~/.kayden`). Only one of them
  can be open at a time. Opening the app again brings the open window forward; when the
  terminal app has it, the app says so once and closes, and `kayden` in a terminal says so
  and exits.
- Closing the window stops running tasks, as quitting the terminal app does.
