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
("Read your notes"), with the exact input below it. The steps between two replies, and the
agent's thinking between them, fold into one line that shows the step under way, then how
many there were and any that failed ("4 steps · 1 failed"); open it to see them all. In the
terminal app, select the line and press Enter.

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

### Proposals

When the agent has an idea of its own (tidying a folder, handing a side task to a
sub-session), it proposes it instead of doing it. A card shows what it would do and why,
with **Approve** and **Dismiss** (`/approve` and `/dismiss` in the terminal app). The agent
carries on meanwhile. Approve runs exactly that step, without asking again, and the agent is
told the result; Dismiss tells it you said no. Proposals still open when you quit, stop the
agent or switch conversations are withdrawn.

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
| | `/back` | Leave the sub-session you opened, back to your conversation |
| | `/approve`, `/dismiss` | Answer the agent's open [proposal](#proposals) |
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

F4 shows long-term memory: About you and the notes Kayden keeps across conversations (see
[Memory](concepts.md)). Kayden writes them after each answer, and the agent saves, reads or
deletes one when it needs to, which shows as a step. In F4 you can add, edit or delete
notes yourself; About you can be emptied but not deleted. Deleting a note does not delete
the messages it came from.

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
sure of. It links the pages its answer relies on.

With Claude as the AI provider, searches run on Anthropic's side: reliable, with the
sources listed under the answer. Each search costs about 1 cent on your Anthropic account,
plus the tokens of the pages it reads, and counts toward your spending limit. If web search
is switched off for your Anthropic account, Kayden uses its own. With other providers,
Kayden's own search goes to DuckDuckGo and other search engines. Pages are fetched from the
sites themselves. Anything the agent has
read can end up in a search or an address, so keep secrets out of what you share with it.
It reads public pages only, not pages on your computer or your local network. Some sites
refuse programs that read them (Kayden says so and looks elsewhere), and search engines
sometimes turn searches away for a while; Kayden tries a search again before it gives up.

## More tools (MCP)

Tool servers give Kayden more tools: GitHub, a database, your calendar, and many more.
They use MCP, the Model Context Protocol, which most AI apps support; servers are listed on
their makers' sites and at github.com/modelcontextprotocol/servers.

To add one, open **Settings → Tool servers** in the desktop app, or `/mcp` in the
terminal, and give it:

- a **name**, such as `github`; its tools appear as `mcp__github__…`;
- a **command** to start it (`npx -y @modelcontextprotocol/server-github`) or its **URL**;
- any **variables** it needs, one per line as `NAME=value`, such as an access token. Values
  are kept in your system's keychain, never in a file; leave a value empty to keep it.

Servers start with the next start of Kayden: press **Restart now**. A server that fails to
start is left out; the others still work.

A tool server is someone else's program running with your permissions, so add only ones
you trust. Kayden treats each of its actions as reaching outside the work folder: it checks
or asks before it runs (see [Permissions](settings.md#permissions)).

## Long-running tools

A tool that takes longer than about 10 seconds moves to the background: the agent can keep
working, and the result arrives in the conversation when it finishes. Ctrl+K lists running
tools so you can cancel one.

## Quitting

Ctrl+Q quits. Tools still running are cancelled; your conversations are saved and can be
continued later.
