# Settings

## The settings screen

Press **F2** (or type `/settings`). Change what you need and choose **Save**; only what you
changed is saved, and most changes apply to the agent's next step.

| Setting | Notes |
|---|---|
| AI provider | Claude, DeepSeek or OpenAI. Can only be switched while the agent is idle. |
| API key | Stored in your system's keychain, never in a file. Leave empty to keep the current one. |
| Model | The list shows each model's price per million tokens. |
| Model for memory upkeep | A cheaper model for summarizing long conversations. |
| Spending limit | In US dollars; empty means no limit. |
| What the agent may do | See [Permissions](#permissions). |
| Instructions | The system prompt: who the agent is and how it should behave. |
| Work folder | Where the agent's commands and file tools work. Can only be changed while the agent is idle. |

Raising or removing the spending limit, or letting the agent do more, asks you to confirm
first.

## Permissions

| Level | The agent |
|---|---|
| Ask me before every action | Asks before every tool call (the default) |
| Read freely, ask before changes | Reads, lists and searches files on its own; asks before commands and changes |
| Never run tools | Can only talk |

## Spending limit

The limit counts everything spent in this mode, across all its conversations, at the
providers' list prices. When it is reached, Kayden stops the current step and refuses new
ones until you raise the limit. The settings screen shows what has been spent so far.

## Where settings live

Everything is in Kayden's data folder, `~/.kayden` (or `--data-dir`, or `$KAYDEN_DATA_DIR`):

| File | Holds |
|---|---|
| `settings.toml` | What you changed in the settings screen |
| `config.toml` | Optional hand-written configuration, for advanced use |

Settings from the screen override `config.toml`, and command-line options override both.
To see the result, with defaults filled in and secrets hidden:

```bash
kayden --show-config
```

## config.toml

For things the settings screen does not cover. Example: DeepSeek with a $5 limit and custom
instructions from a file next to the config:

```toml
[core]
factory = "kayden.core.openai_compat:factory"
options = { profile = "deepseek" }

[runtime]
model = "deepseek-flash"
maintenance_model = "deepseek-flash"
permissions = "ask"                 # ask | allow_read_only | deny
system_prompt_file = "prompt.md"    # or: system_prompt = "..."

[runtime.budget]                    # each limit optional
max_cost_usd = 5.0
max_tokens = 1000000
max_duration_s = 3600               # time spent working
max_tool_calls = 200
```

`[core]`, `[memory]`, `[tools]` and `[frontend]` each take a `factory` and `options`; a
section left out uses Kayden's default. The output of `kayden --show-config` is a valid
`config.toml` to start from. A mistake in the file stops Kayden with a one-line message
naming it.

## API keys

Kayden looks for a key in the environment first (`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`,
`OPENAI_API_KEY`), then in the keychain where the settings screen stores it. Keys never go
into Kayden's files or logs.
