# Using Kayden

## Starting

`kayden` opens the start screen: pick an existing conversation or a new one, and the model,
then press Enter. A new conversation can **Continue from** an earlier one, starting from
where that one left off, and can work in another **Folder** (empty: the default folder).

To skip the screen:

```bash
kayden --conversation ID       # continue a conversation
```

| Option | Does |
|---|---|
| `--conversation ID` | Start right away in this conversation |
| `--model ID`, `--maintenance-model ID` | Override the models for this run |
| `--permissions auto\|allow_workspace\|allow_read_only\|ask\|deny` | Override the [permission level](settings.md#permissions) for this run |
| `--data-dir DIR`, `--config FILE` | Use another data folder or config file |
| `--show-config` | Print the settings Kayden would use, then exit |

## Talking to the agent

Type a message and press Enter. While the agent works you can keep typing; your messages
are queued and delivered when it is ready. Its reasoning, replies and tool calls appear in
the conversation as they happen. Each step reads as a short sentence the agent writes
("Read your notes"), with the exact input below it.

## Approving actions

When the agent wants to use a tool that the permission level does not allow on its own,
Kayden asks. The question starts with what the agent says the step does ("Remove the old
build folder"), followed by its exact input. That sentence is the agent's own words: if it
doesn't match the input, trust the input.

| Answer | Effect |
|---|---|
| Allow once | Runs this one call |
| Allow for this conversation | Runs this tool without asking again in this conversation, until you quit |
| Deny | The agent is told the call was refused |

The permission level is set in [Settings](settings.md#permissions).

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
| | `/folder [path]` | Show the work folder, or start a chat in another one |
| | `/allow [once\|conversation]`, `/deny` | Answer the open approval |
| Ctrl+Q | `/quit` | Quit |
| | `/help` | List the commands |

## Conversations and memory

F3 lists your conversations, newest first; "Show archived" includes archived ones. Open
one, start a new one, rename it (leave the name empty for an automatic one), archive it to
hide it, or delete it for good. Deleting asks first and cannot be undone; deleting the
open conversation opens a new chat in its place. You can only switch
conversations while the agent is not working. See [Deleting](concepts.md#deleting) for what
deletion does and does not reach.

F4 shows long-term memory: facts Kayden keeps across conversations. The agent adds one when
you ask it to remember something or tell it something lasting about yourself, and removes
one when you say it is no longer true; you see a `remember` or `forget` step in the
conversation. In F4 you can add, correct or forget them yourself. Forgetting a fact does not delete the messages it came from.

## Folders

Each conversation works in one folder: its commands run there and its files are read and
written there. A new conversation gets the folder from Settings (**Folder for new chats**),
or the one you type in the start screen; it keeps that folder when you come back to it,
even after you change the setting. The status line shows it.

`/folder PATH` starts a chat in another folder. If the open chat has no messages yet, it is
replaced, so no empty chat is left behind. **Continue from** keeps the earlier
conversation's folder.

## The web

The agent can search the web and read web pages, for anything recent or that it is not
sure of. It links the pages its answer relies on. Searches go to DuckDuckGo and other
search engines, and pages are fetched from the sites themselves. Anything the agent has
read can end up in a search or an address, so keep secrets out of what you share with it.
It reads public pages only, not pages on your computer or your local network.

## Long-running tools

A tool that takes longer than about 10 seconds moves to the background: the agent can keep
working, and the result arrives in the conversation when it finishes. Ctrl+K lists running
tools so you can cancel one.

## Quitting

Ctrl+Q quits. Tools still running are cancelled; your conversations are saved and can be
continued later.
