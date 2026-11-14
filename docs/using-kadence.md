# Using Kadence

## Starting

`kadence` opens the start screen: pick an existing conversation or a new one, and the model,
then press Enter. A new conversation can **Continue from** an earlier one, starting from
where that one left off, and can work in another **Folder** (empty: the default folder).

To skip the screen:

```bash
kadence --conversation ID       # continue a conversation
```

| Option | Does |
|---|---|
| `--conversation ID` | Start right away in this conversation |
| `--model ID`, `--maintenance-model ID` | Override the models for this run |
| `--permissions auto\|allow_workspace\|allow_read_only\|ask\|deny` | Override the [permission level](settings.md#permissions) for this run |
| `--data-dir DIR`, `--config FILE` | Use another data folder or config file |
| `--show-config` | Print the settings Kadence would use, then exit |

## Talking to the agent

Type a message and press Enter. While the agent works you can keep typing; your messages
are queued and delivered when it is ready. Its reasoning, replies and tool calls appear in
the conversation as they happen. Each step reads as a short sentence the agent writes
("Read your notes"), with the exact input below it. The steps between two replies, and the
agent's thinking between them, fold into one line that shows the step under way, then how
many there were and any that failed ("4 steps · 1 failed"); open it to see them all. In the
terminal app, select the line and press Enter.

### Talking with Kadence

In the desktop app you can talk instead of typing. Click the sound-wave button in the
message box:

- **You talk, Kadence answers aloud.** It sends what you said once you have been silent for
  about a second, and reads the reply while it is still being written. The conversation
  is on screen as usual, and you can type in between.
- **Only words are read.** Code, tables, links and symbols stay on screen.
- **Esc stops the reading** and Kadence listens again. You cannot talk over it: while it
  speaks, it does not listen.
- **The agent knows you are talking**, so it answers briefly, in plain sentences, without
  emoji, kaomoji or symbols, until you type again.
- **Speech runs on your computer.** What you say is not sent to a speech service; only
  the text goes to the model, as when you type. The first time, Kadence asks to download
  the speech models (about 830 MB) into its data folder.
- **English only** for now.

### Attaching files

In the desktop app, click **+** in the message box, drop files on the window or paste
them, then send. A message can be
attachments only.

- **A picture** (PNG, JPEG, GIF or WebP, up to 5 MB) is shown to the model, so you can ask
  about a screenshot or a photo. With a model that cannot see pictures (DeepSeek), the
  agent is only told that one was attached.
- **A PDF** (up to 20 MB and 600 pages) goes to Claude whole: it reads the text and sees
  every page, so tables, charts and scans work. Other providers get the text of its pages.
- **A Word, Excel or PowerPoint file** (.docx, .xlsx, .pptx) goes as its text: a
  document's headings, paragraphs and tables, a workbook's sheets row by row, a deck's
  slides with their notes. Formatting, pictures and charts in them are not included.
- **A text file** (up to 200 kB) goes with its content and its path, so the agent can read
  it at once and knows where it is, also when it is outside the work folder.
- **Any other file** (binary, or larger) goes by its path only; the agent opens it with
  its tools if it needs to.

Attachments are kept with the conversation and shown again when you reopen it. A picture
or a PDF adds to what every later message of that conversation costs; a PDF is charged
by the page.

The agent can also open these by itself when it comes across them, with its file tool:

- **A picture** is shown to the model, so it can look at a screenshot or a diagram in your
  project.
- **A PDF** is read as the text of its pages, all of them or a range. When the text is
  not enough (a scan, a chart, a layout), the agent can ask to look at the pages
  themselves, up to 20 at a time. That costs far more, and only Claude sees them; other
  providers get the text again.
- **A Word, Excel or PowerPoint file** is read as its text.

In the list of steps, such a result names what it returned (`[image: chart.png]`). The terminal app shows what
was attached but cannot attach.

## Approving actions

When the agent wants to use a tool that the permission level does not allow on its own,
Kadence asks. The question starts with what the agent says the step does ("Remove the old
build folder"), followed by its exact input. That sentence is the agent's own words: if it
doesn't match the input, trust the input.

| Answer | Effect |
|---|---|
| Allow once | Runs this one call |
| Allow for this conversation | Runs this tool without asking again in this conversation, until you quit |
| Deny | The agent is told the call was refused |

The permission level is set in [Settings](settings.md#permissions).

### Proposals

When the agent has an idea of its own (tidying a folder, handing a side task to a
sub-session), it proposes it instead of doing it. A card shows what it would do and why,
with **Approve** and **Dismiss** (`/approve` and `/dismiss` in the terminal app). The agent
carries on meanwhile. Approve runs exactly that step, without asking again, and the agent is
told the result; Dismiss tells it you said no. Proposals still open when you quit, stop the
agent or switch conversations are withdrawn.

### The plan

For a task with several steps, the agent writes a plan first and ticks the steps off as it
works: ✓ done, ◐ under way, ○ still to do, … waiting for you (two bars in the desktop app), and struck through for
steps it dropped. A quick
question gets no plan.

- **In the desktop app** the checklist is part of the conversation. It sits under the
  newest message while the agent works and is gone when the agent stops.
- **In the terminal app** it shows above the message box while the agent works.
- **The agent finishes its plan before it stops.** If it ends a turn with steps open, Kadence
  sends it back once to do them or close them. A step can stay open when it waits for
  you, for a reminder's time, or for a duty's next check-in.

### Reminders

Ask Kadence to remind you of something, or to do something later ("remind me at 9 to call
Sam", "check the build again in ten minutes"). It gives a step of its plan a due time, and
is woken when the time comes, without a message from you. The status line shows
**Reminder at 09:00** while one is waiting.

- **The conversation has to be open in Kadence** at that time. A reminder that came due
  while it was closed, or while Kadence was not running, is not sent late by itself: the
  agent sees it with your next message.
- **Stop** puts reminders off until your next message.
- For work that keeps going on its own, give the conversation a
  [duty](concepts.md#duties) instead.

### The wrap-up card

When the agent needs you to decide something, it ends its turn with a card above the message
box: the question, up to three buttons with its recommendation first, and sometimes a short
recap of what is done and what is not. Other answers have no card.

- Most buttons send their text as your message, as if you had typed it.
- One button may do the thing itself ("Yes, push" runs the push). Kadence runs exactly what
  the agent proposed, and the agent picks up from the result.
- Type any other answer instead, or close the card with ✕. Once you send anything, the card
  goes away, and a button that would have done something is withdrawn.
- The card comes back if you reopen the conversation before answering. After a restart a
  button that would have done something just sends its text.

## Keys and commands

| Key | Command | Does |
|---|---|---|
| Enter | | Send the message |
| Esc | `/stop` | Stop the agent; running tools are cancelled |
| Ctrl+R | `/activate` | Let the agent continue without a new message |
| Ctrl+O | `/model [id]` | Switch the model |
| F2 | `/settings` | Open the settings screen |
| Ctrl+K | `/cancel [id]` | Cancel one running tool |
| F3 | `/conversations` | Open, start, rename, archive or delete conversations |
| F4 | `/memory` | See, add, edit or forget long-term memory |
| | `/mcp` | Add, edit or remove tool servers (MCP) |
| F5 | `/duty`, `/plan` | Open the duty and plan of this conversation ([Duties](concepts.md#duties)) |
| | `/duty <what to do>`, `/goal <what to do>` | Give this conversation a duty that keeps going, or one that ends when done |
| | `/duty off` | End the duty; the plan stays |
| | `/sessions` | This conversation's [sub-sessions](concepts.md#sub-sessions): open one in place, resume, stop, change its spending limit (or the limit per sub-session), rename, archive or delete |
| | `/back` | Leave the sub-session you opened, back to your conversation. One that ends takes you back by itself |
| | `/approve`, `/dismiss` | Answer the agent's open [proposal](#proposals) |
| | `/folder [path]` | Show the work folder, or start a chat in another one |
| | `/allow [once\|conversation]`, `/deny` | Answer the open approval |
| Ctrl+Q | `/quit` | Quit |
| | `/skills` | List the [skills](concepts.md#skills) |
| | `/<skill name>` | Run a skill, for example `/consolidate-memory` |
| | `/help` | List the commands |

## Conversations and memory

F3 lists your conversations, newest first; "Show archived" includes archived ones. A new
chat is listed once you send its first message, and is not kept if you leave it before.
Open one, start a new one, rename it (leave the name empty for an automatic one), archive it to
hide it, or delete it for good. Deleting asks first and cannot be undone; deleting the
open conversation opens a new chat in its place. You can only switch
conversations while the agent is not working. See [Deleting](concepts.md#deleting) for what
deletion does and does not reach.

F4 shows long-term memory: About you and the notes Kadence keeps across conversations (see
[Memory](concepts.md)). Kadence writes them after each answer, and the agent saves, reads or
deletes one when it needs to, which shows as a step. In F4 you can add, edit or delete
notes yourself; About you can be emptied but not deleted. Deleting a note does not delete
the messages it came from.

## Folders

Each conversation works in one folder: its commands run there and its files are read and
written there. A new conversation gets the folder from Settings (**Folder for new chats**),
or the one you type in the start screen; it keeps that folder when you come back to it,
even after you change the setting. The status line shows it.

`/folder PATH` starts a chat in another folder. **Continue from** keeps the earlier
conversation's folder.

## The web

The agent can search the web and read web pages, for anything recent or that it is not
sure of. It links the pages its answer relies on.

With Claude as the AI provider, searches run on Anthropic's side: reliable, with the
sources listed under the answer. Each search costs about 1 cent on your Anthropic account,
plus the tokens of the pages it reads, and counts toward your spending limit. If web search
is switched off for your Anthropic account, Kadence uses its own. With other providers,
Kadence's own search goes to DuckDuckGo and other search engines. Pages are fetched from the
sites themselves. Anything the agent has
read can end up in a search or an address, so keep secrets out of what you share with it.
It reads public pages only, not pages on your computer or your local network. Some sites
refuse programs that read them (Kadence says so and looks elsewhere), and search engines
sometimes turn searches away for a while; Kadence tries a search again before it gives up.

## Looking at your screen

Ask the agent to look at your screen ("what does this error on my screen mean?") and it
takes a screenshot of your whole desktop and sees it, in the terminal app, the desktop
app and when you talk to it.

- **It asks first**, like any action outside the work folder. At the permission level
  Auto it looks without asking when your message asks for it.
- **Everything on screen is sent to the model**: close what you do not want it to see.
- **It only looks.** It cannot click or type on your desktop.
- **KDE Plasma only** for now: it uses Spectacle, KDE's screenshot program. On other
  desktops the agent does not have this tool.

A screenshot costs about as much as a page of text each time, and stays in the
conversation like an attached picture.

## More tools (MCP)

Tool servers give Kadence more tools: GitHub, a database, your calendar, and many more.
They use MCP, the Model Context Protocol, which most AI apps support; servers are listed on
their makers' sites and at github.com/modelcontextprotocol/servers.

To add one, open **Settings → Tool servers** in the desktop app, or `/mcp` in the
terminal, and give it:

- a **name**, such as `github`; its tools appear as `mcp__github__…`;
- a **command** to start it (`npx -y @modelcontextprotocol/server-github`) or its **URL**;
- any **variables** it needs, one per line as `NAME=value`, such as an access token. Values
  are kept in your system's keychain, never in a file; leave a value empty to keep it.

Servers start with the next start of Kadence: press **Restart now**. A server that fails to
start is left out; the others still work.

A tool server is someone else's program running with your permissions, so add only ones
you trust. Kadence treats each of its actions as reaching outside the work folder: it checks
or asks before it runs (see [Permissions](settings.md#permissions)).

## Long-running tools

A tool that takes longer than about 10 seconds moves to the background: the agent can keep
working, and the result arrives in the conversation when it finishes. Ctrl+K lists running
tools so you can cancel one.

## Quitting

Ctrl+Q quits. Tools still running are cancelled; your conversations are saved and can be
continued later.
