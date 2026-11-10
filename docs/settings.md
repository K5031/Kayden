# Settings

## The settings screen

Press **F2** (or type `/settings`); in the [desktop app](desktop-app.md), choose
**Kayden → Settings**. Change what you need and choose **Save**; only what you
changed is saved.

| Setting | Notes | Applies |
|---|---|---|
| AI provider | Claude, DeepSeek, OpenAI, or another one you installed. | At restart |
| API key | Stored in your system's keychain, never in a file. Leave empty to keep the current one. | At restart |
| Model | The list shows each model's price per million tokens. | At the next step |
| Model for memory upkeep | A cheaper model for summarizing long conversations. | At the next step |
| Spending limit | In US dollars; empty means no limit. | At once |
| What the agent may do | See [Permissions](#permissions). | At the next step |
| Instructions | The system prompt: who the agent is and how it should behave. | At the next step |
| Work folder | Where the agent's commands and file tools work. | At restart |
| Memory | Where conversations and long-term memory are kept. | At restart |
| Tools | What the agent can do on your computer. | At restart |
| Interface | The terminal app or the [desktop app](desktop-app.md). | At restart |

Raising or removing the spending limit, or letting the agent do more, asks you to confirm
first.

## Changes that apply at restart

The AI provider, its key, the work folder, Memory, Tools and Interface are saved at once but
used from the next start of Kayden; until then you keep working as before. The settings
screen says so and offers **Restart now**: Kayden stops what it is doing, as when you quit,
and starts again with the new settings in the same conversation.

After a provider change, the new provider starts with its first model; choose another one
after the restart.

**Switching between the terminal and the desktop app.** Choose the interface, then
**Restart now**:

- From the terminal app, choosing the desktop app closes the terminal app and opens the
  desktop app in the same conversation. Without a screen to show it on (an SSH session, for
  example), the terminal app says the desktop app will be used the next time you start
  Kayden on a computer with a display.
- The desktop app cannot open a terminal. Choosing the terminal app there means it is used
  the next time you run `kayden` in a terminal; **Restart now** restarts the desktop app for
  any other change.

With the desktop app chosen, running `kayden` opens the desktop app instead of the terminal
app, and says so. It opens the terminal app, saying why, only when there is no screen to
show a window on or the desktop app is not installed. The desktop app, started from the app
menu, always opens a window, even with the terminal app chosen. Both need to be installed;
see [Desktop app](desktop-app.md#install).

**A different Memory is a different archive.** Your conversations and long-term memory stay
where they are, but are not shown while another Memory is used; they come back when you
switch back. The settings screen asks before saving such a change.

## When Kayden cannot start

If Kayden cannot start with your settings (no API key yet, a provider or module that is no
longer installed, wrong options in `config.toml`), it still opens, with the settings and a
message saying what is wrong. Fix it there and choose **Restart now**. This is also what
the first start looks like: enter your API key, then **Restart now**.

## Installed modules

The provider, Memory, Tools and Interface lists show every module installed with Kayden:
its own, and those of any package you install that provides one. A module is a program
that runs with your permissions, so install only ones you trust; see
[Development](development.md#writing-a-module).

## Permissions

| Level | The agent |
|---|---|
| Ask me before every action | Asks before every tool call (the default) |
| Read freely, ask before changes | Reads, lists and searches files on its own; asks before commands and changes |
| Never run tools | Can only talk |

## Spending limit

The limit counts everything spent in this data folder, across all conversations, at the
providers' list prices. When it is reached, Kayden stops the current step and refuses new
ones until you raise the limit. The settings screen shows what has been spent so far.

## Where settings live

Everything is in Kayden's data folder, `~/.kayden` (or `--data-dir`, or `$KAYDEN_DATA_DIR`):

| File | Holds |
|---|---|
| `settings.toml` | What you changed in the settings screen, the chosen modules under `[modules]` |
| `config.toml` | Optional hand-written configuration, for advanced use |
| `memory.db` | Your conversations and long-term memory |
| `usage.json` | What has been spent so far; delete it, while Kayden is closed, to start counting from zero |
| `events.jsonl` | Activity log of everything Kayden did, messages included |

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
factory = "kayden.core.openai_compat:deepseek_factory"

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
section left out uses Kayden's default. A module chosen in the settings screen replaces the
one named here, keeping these `options` only when it is the same module. Options a module
understands (prices, context windows, base URLs, compaction) are set here only, and apply
at the next start. The output of `kayden --show-config` is a valid `config.toml` to start
from. A file that is not valid stops Kayden with a one-line message naming the mistake; a
module that rejects its options opens the settings with the message instead.

## API keys

Kayden looks for a key in the environment first (`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`,
`OPENAI_API_KEY`), then in the keychain where the settings screen stores it. Keys never go
into Kayden's files or logs.
