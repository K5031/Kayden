# Using Kayden

## Starting

`kayden` opens the start screen: pick an existing conversation or a new one, and the model,
then press Enter. For a new conversation you can also set:

| Field | Does |
|---|---|
| Autonomous mode | The agent works on your task without stopping to ask, and carries on when its background work finishes (see [Autonomy](concepts.md#autonomy)) |
| Continue from | Start from where an earlier conversation left off |

To skip the screen:

```bash
kayden --conversation ID       # continue a conversation
kayden --autonomous            # a new autonomous conversation
```

| Option | Does |
|---|---|
| `--conversation ID` | Start right away in this conversation |
| `--autonomous`, `--no-autonomous` | Start a new conversation right away, autonomous or not; cannot be combined with `--conversation` |
| `--model ID`, `--maintenance-model ID` | Override the models for this run |
| `--permissions ask\|allow_read_only\|deny` | Override the [permission level](settings.md#permissions) for this run |
| `--data-dir DIR`, `--config FILE` | Use another data folder or config file |
| `--show-config` | Print the settings Kayden would use, then exit |

## Talking to the agent

Type a message and press Enter. While the agent works you can keep typing; your messages
are queued and delivered when it is ready. Its reasoning, replies and tool calls appear in
the conversation as they happen.

## Approving actions

When the agent wants to use a tool that the permission level does not allow on its own,
Kayden asks:

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

F4 shows long-term memory: facts Kayden keeps across conversations. Add one, correct one,
or forget one. Forgetting a fact does not delete the messages it came from.

## Long-running tools

A tool that takes longer than about 10 seconds moves to the background: the agent can keep
working, and the result arrives in the conversation when it finishes. Ctrl+K lists running
tools so you can cancel one.

## Quitting

Ctrl+Q quits. Tools still running are cancelled; your conversations are saved and can be
continued later.
