# Driving Kayden from a program

`kayden --headless` runs Kayden without a screen, for another program to drive: a fleet
manager, a script, a test. The program starts Kayden as a child process and talks to it
over Kayden's stdin and stdout.

```bash
kayden --headless --data-dir ~/units/writer --goal "Draft the launch post"
```

| Option | Does |
|---|---|
| `--headless` | Use stdin and stdout for the program, as below |
| `--duty "…"` | Start a new conversation with this [duty](concepts.md#duties) right away |
| `--goal "…"` | The same, as a goal: it ends when the agent says it is reached |
| `--data-dir`, `--conversation`, `--model`, `--permissions` | As usual |

Give each unit its own data folder. Its `config.toml` sets the model, permissions and
spending limit (a duty needs one); see [config.toml](settings.md#configtoml).

## The channel

[JSON-RPC 2.0](https://www.jsonrpc.org/specification), one message per line, UTF-8.
Kayden writes nothing else to stdout: anything else the process prints goes to stderr, and
so do Kayden's own errors, such as a duty that cannot start. Closing Kayden's stdin quits it
the way closing the window does.

**Requests** have the names and parameters of the runtime's commands:

| Method | Params |
|---|---|
| `start` | `conversation_id`, `model`, `seed_from`, `folder` (all optional) |
| `send` | `text` |
| `activate`, `stop` | none |
| `cancel_operation` | `call_id` |
| `select_model` | `model_id` |
| `set_duty` | `duty`: `{instruction, kind ("duty" or "goal"), min_interval_s, max_interval_s}` or `null` to end it |
| `set_plan` | `plan`: a list of `{text, status, due, note}` |
| `resolve_permission` | `request_id`, `decision` (`allow_once`, `allow_conversation`, `deny`) |
| `snapshot` | none |
| `history` | `conversation_id`, optional `before`, `limit` |
| `list_conversations` | optional `include_archived` |
| `list_models`, `settings` | none |

A refusal (asking before `start`, a duty without a spending limit, an unknown
conversation) is error `-32000` with the runtime's message and `data.type`, such as
`RuntimeStateError`. Missing parameters are `-32602`, unknown methods `-32601`.

**Events** come as notifications: `{"method": "event", "params": {"seq": 12, "event":
{...}}}`, every event the runtime emits, in order. Values are JSON objects with their kind
under `"type"`, such as `MessageCommitted`, `StateChanged`, `PermissionRequested`,
`HelpNeeded` and `DutyScheduled`. A jump in `seq` means events were dropped: take a
`snapshot`, ignore events up to its `seq`, and read `history`.

**Prompts reach the program.** A tool call that needs approval comes as
`PermissionRequested`: answer it with `resolve_permission`. A question the agent asks
with its duty paused comes as `HelpNeeded`: answer it with `send`.

## Example

```
→ {"jsonrpc": "2.0", "id": 1, "method": "start", "params": {}}
← {"jsonrpc": "2.0", "id": 1, "result": {"type": "ConversationInfo", "id": "c41…", …}}
→ {"jsonrpc": "2.0", "id": 2, "method": "send", "params": {"text": "Summarize notes.md"}}
← {"jsonrpc": "2.0", "id": 2, "result": null}
← {"jsonrpc": "2.0", "method": "event", "params": {"seq": 3, "event": {"type": "StateChanged", "state": "thinking", "pending_ops": 0}}}
← … TextDelta, MessageCommitted, StateChanged idle …
```
