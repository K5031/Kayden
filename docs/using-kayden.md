# Using Kayden

## Starting

`kayden` opens the start screen: choose the mode (see [Concepts](concepts.md#modes)), an
existing conversation or a new one, and the model, then press Enter. To skip the screen:

```bash
kayden --mode session                      # a new conversation
kayden --mode session --conversation ID    # continue one
kayden --mode continuous                   # the always-on agent
```

| Option | Does |
|---|---|
| `--mode session\|continuous` | Start right away in this mode |
| `--activation user_controlled\|agent_controlled` | Continuous mode's [activation](concepts.md#activation-continuous-mode) |
| `--conversation ID` | Continue this session conversation |
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
| Allow for conversation | Runs this tool without asking again in this conversation, until you quit |
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
| | `/allow [once\|conversation]`, `/deny` | Answer the open approval |
| Ctrl+Q | `/quit` | Quit |
| | `/help` | List the commands |

## Long-running tools

A tool that takes longer than about 10 seconds moves to the background: the agent can keep
working, and the result arrives in the conversation when it finishes. Ctrl+K lists running
tools so you can cancel one.

## Quitting

Ctrl+Q quits. Tools still running are cancelled; your conversations are saved and can be
continued later.
