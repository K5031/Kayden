# Settings

## The settings screen

Press **F2** (or type `/settings`); in the [desktop app](desktop-app.md), choose
the gear at the bottom left (or **Ctrl+,**). Change what you need and choose **Save**; only what you
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
| Folder for new chats | The folder a new conversation works in. Each conversation keeps its own; see [Folders](using-kayden.md#folders). | At the next new chat |
| Tools | What the agent can do on your computer. | At restart |

Raising or removing the spending limit, or letting the agent do more, asks you to confirm
first.

## Changes that apply at restart

The AI provider, its key, Tools and tool servers (MCP) are saved at once but
used from the next start of Kayden; until then you keep working as before. The settings
screen says so and offers **Restart now**: Kayden stops what it is doing, as when you quit,
and starts again with the new settings in the same conversation.

After a provider change, the new provider starts with its first model; choose another one
after the restart.

**The terminal app or the desktop app** is not a setting: `kayden` starts the terminal
app, and `kayden-desktop` or Kayden in your app menu starts the
[desktop app](desktop-app.md). Both show the same conversations and use the same settings.

**A different Memory is a different archive.** Your conversations and long-term memory stay
where they are, but are not shown while another Memory is used; they come back when you
switch back. The settings screen asks before saving such a change.

## When Kayden cannot start

If Kayden cannot start with your settings (no API key yet, a provider or module that is no
longer installed, wrong options in `config.toml`), it still opens, with the settings and a
message saying what is wrong. Fix it there and choose **Restart now**. This is also what
the first start looks like: enter your API key, then **Restart now**.

## Installed modules

The provider and Tools lists show every module installed with Kayden: its own,
and those of any package you install that provides one. A module is a program that runs
with your permissions, so install only ones you trust; see
[Writing a module](writing-modules.md).

The memory module, where conversations and long-term memory are kept, is not in the
settings screen: another one would be another archive, without your conversations. It is
set with `[memory]` in [config.toml](#configtoml). Kayden comes with two:

- **Notes** (the default): About you and one note per topic, shared by all your
  conversations and saved after each answer. See [Memory](concepts.md#memory).
- **Files per project**, as in Claude Code: each project gets its own memory
  folder, `~/.kayden/projects/<project>/memory/`, with a `MEMORY.md` index and one
  Markdown file per fact. A project is a git repository, so chats in its subfolders and
  worktrees share one memory; a folder outside any repository is its own project. The agent writes them itself while it works, and every
  conversation in that folder sees the index as it was when the conversation started: a
  memory saved or edited meanwhile shows up in the next conversation. The agent reads a
  memory before it changes it and edits it in place. A conversation gets the first 200
  lines of the index, so the agent is told to condense it when it grows past that. Choose
  it with:

  ```toml
  [memory]
  factory = "kayden.memory.files:factory"
  ```

  It keeps its conversations in `memory-files.db`, so it starts empty; your other
  conversations come back when you remove these lines.

## Permissions

| Level | The agent |
|---|---|
| Decide automatically, ask me only when unsure | Works freely in its work folder; a quick automatic check decides about anything outside it, and asks you when it is unsure (the default) |
| Work freely in the work folder, ask before anything else | Reads and changes files in its work folder and runs commands there without asking; asks before anything outside it |
| Read freely, ask before changes | Reads, lists and searches files anywhere on its own; asks before commands and changes |
| Ask me before every action | Asks before every tool call |
| Never run tools | Can only talk |

**Deciding automatically.** When the agent wants to do something outside its work folder
(run a command outside the sandbox, read a file elsewhere, push to GitHub), Kayden first
asks the model for memory upkeep whether it fits what you asked for. It runs without
asking when it clearly does and is easy to undo, or only reads. Kayden asks you when it
deletes or overwrites something you did not mention, sends data anywhere, publishes,
spends money, changes system settings, or when the check is unsure; the question then
says why. If the check fails or the spending limit is reached, Kayden asks too.

- The check sees your messages and the action, not what the agent read, so a web page or
  a file cannot talk it into allowing something.
- Anything that touches keys and passwords (SSH and GPG keys, `.env` files, shell
  startup files such as `~/.bashrc`, password stores, cloud credentials, browser
  profiles, the keychain) or Kayden's own data folder
  always asks you, without a check: a secret the agent reads goes to your AI provider, and
  Kayden's data folder holds your conversations and the very settings that limit the
  agent.
- Each check is a small request to your provider and counts toward the spending limit. Set
  a cheap **Model for memory upkeep** to keep it cheap.
- It can be wrong. Choose "Work freely in the work folder, ask before anything else" to
  decide everything outside the folder yourself.

**How "in the work folder" is enforced.** A file action counts as inside when its path is
in the work folder; a link that leads out of it counts as outside. Commands run in a
sandbox ([bubblewrap](https://github.com/containers/bubblewrap)) in which they can:

- read and change the work folder;
- read the system, so installed programs run;
- use the network.

They cannot change anything else, and cannot see your home folder, other programs, your
desktop session (so not your keychain either) or environment variables whose names look
like secrets (API keys, tokens, passwords). Searching the web and reading web pages also count as inside: they touch
nothing on your computer. When a command needs more, for example a tool installed in your home
folder, the agent asks to run it outside the sandbox, and the question says so.

Things to know:

- **Keep secrets out of the work folder.** A command there can send the folder's contents
  anywhere.
- **Without the sandbox, commands ask.** That is the case on macOS and on Linux without
  bubblewrap; file actions in the work folder still run freely.
- **Your home folder is not a work folder.** If the work folder is your home folder, or a
  folder that contains it, nothing counts as inside and every action asks.

## Spending limit

The limit counts everything spent in this data folder, across all conversations, at the
providers' list prices. When it is reached, Kayden stops the current step and refuses new
ones until you raise the limit. The settings screen shows what has been spent so far.

## Where settings live

Everything is in Kayden's data folder, `~/.kayden` (or `--data-dir`, or `$KAYDEN_DATA_DIR`):

| File | Holds |
|---|---|
| `settings.toml` | What you changed in the settings screen, the chosen modules under `[modules]`, tool servers under `[[mcp_servers]]` (their variables by name only) |
| `config.toml` | Optional hand-written configuration, for advanced use |
| `memory.db` | Your conversations and long-term memory |
| `memory-files.db`, `projects/` | The same, with the files-per-project memory |
| `skills/` | Your own [skills](concepts.md#skills) |
| `usage.json` | What has been spent so far; delete it, while Kayden is closed, to start counting from zero |
| `events.jsonl` | Activity log of what Kayden did and when; it holds no message text, tool input or tool output |

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
permissions = "auto"                # auto | allow_workspace | allow_read_only | ask | deny
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

With Claude models that always think (Opus 5.x, Sonnet 5.x, Haiku 5.x), Kayden asks for a
summary of the thinking to be streamed, so you see it under **Thinking…** as it happens.
To change that, set `thinking` in the Core's options for that model, for example
`thinking = { type = "adaptive", display = "omitted" }`.

With Claude, Kayden asks Anthropic to cache each conversation, so every request after the
first pays about a tenth or less for what was already sent. The cache lasts five minutes
after the last request. For work with longer pauses (a duty that checks in every half
hour), keep it for an hour instead, at a higher price for the first request:

```toml
[core.options.options]
cache_control = { type = "ephemeral", ttl = "1h" }
```

## API keys

Kayden looks for a key in the environment first (`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`,
`OPENAI_API_KEY`), then in the keychain where the settings screen stores it. Keys never go
into Kayden's files or logs. Variables of tool servers are kept in the keychain too, as
`mcp/<server>/<NAME>`.
