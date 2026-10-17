# Kayden

A modular desktop AI assistant. Its `kayden` command runs a controller that connects
a selectable frontend, model, context, memory, action strategy, and tool modules.

## Installation

```bash
git clone https://github.com/K5031/Kayden.git
cd Kayden
python3 -m venv venv
source venv/bin/activate
python -m pip install -e .
```

The base installation supports the TUI, desktop/floaty Tk frontends, DeepSeek,
OpenAI API, and ChatGPT plan modules. Install only the other optional features
you need:

```bash
python -m pip install -e ".[local]"         # local llama.cpp core
python -m pip install -e ".[memory]"        # legacy Remis memory dependencies
python -m pip install -e ".[voice-input]"   # Whisper microphone input
python -m pip install -e ".[voice-output]"  # Kokoro speech output
python -m pip install -e ".[all]"           # everything
```

If you'll use a local core or memory module, drop the GGUF models you want into `models/` when running from a checkout. A regular installation uses `$XDG_DATA_HOME/k/models/` (normally `~/.local/share/k/models/`).

Run it:

```bash
kayden
# Or, from a source checkout: python src/main.py
```

On first run this creates `~/.config/kayden/config.toml` and
`~/.config/kayden/system_prompt.txt` from defaults bundled with the package.
An existing `~/.config/k` directory is migrated automatically. Edit
`config.toml` to pick your modules and `system_prompt.txt` to customize the
assistant's personality.

Some modules need credentials. The `deepseek` core reads `DEEPSEEK_API_KEY`,
while the `openai` core reads `OPENAI_API_KEY`. The separate `chatgpt` core opens
the official Continue with ChatGPT flow and uses an eligible Plus or Pro plan.

## Configuration

The default configuration uses DeepSeek, the full-screen TUI, continuous local
history, and the starter tool set:

```toml
controller = "cloud"
frontend = "tui"
core = "deepseek"
context = "cloud"
memory = "continuity"
action = "native"
tools = ["clock", "calculator", "filesystem", "web"]
```

For fully local inference, install the `local` extra and switch the relevant
settings:

```toml
controller = "local"
core = "llama_cpp"
context = "local"
tools = []
```

The local core does not support native tool calling yet.

To use OpenAI instead, keep the cloud controller/context and set:

```toml
core = "openai"
```

Then provide `OPENAI_API_KEY` in the environment.

To use your ChatGPT plan instead of API billing, set:

```toml
core = "chatgpt"
```

On first launch Kayden opens the system browser for ChatGPT sign-in and plan-use
consent. It stores the issued OAuth registration and rotating tokens in
`~/.config/kayden/chatgpt.json` with owner-only permissions on Unix. Kayden uses
the first model visible to the connected account; set `KAYDEN_CHATGPT_MODEL` to
request a specific available model. This connection does not give Kayden access
to ChatGPT conversations or memories. Disconnect the app in ChatGPT settings if
the local credentials are ever exposed.

Choose the user experience independently from the rest of Kayden:

```toml
frontend = "tui"     # full-screen terminal interface
frontend = "gui"     # desktop chat window
frontend = "floaty"  # compact always-on-top window
```

The graphical frontends use Python's Tk support. On Linux, install your
distribution's `python3-tk` package if it is not already available.

Each key is a capability, and its value is the module (filename, no `.py`) that fills it, loaded from `src/modules/<capability>/`. The `controller` module must be listed first — it declares which capabilities it needs and which are optional. `Registry.from_config` checks the config against those declarations.

Every module named in `tools` contributes its concrete tools to the selected action
strategy. Other main capabilities take one module. The legacy `input` and `output`
capabilities are optional auxiliaries for voice: for example, add `input = "whisper"`
or `output = "kokoro"` alongside any frontend. Old terminal input/output configs are
automatically normalized to `frontend = "tui"` when loaded.

## Built-in Modules

| Capability | Module | Description |
|---|---|---|
| controller | `local` | Runs frontend events against a local core |
| controller | `cloud` | Runs frontend events and feeds reported usage into context trimming |
| frontend | `tui` | Full-screen terminal chat with streaming, scrolling, and interruption |
| frontend | `gui` | Full desktop chat window |
| frontend | `floaty` | Compact always-on-top assistant window |
| input | `whisper` | Optional microphone input available to every frontend |
| output | `kokoro` | Optional Kokoro speech output available to every frontend |
| core | `llama_cpp` | Local inference via llama.cpp, using a GGUF model from `models/` |
| core | `deepseek` | Cloud inference via the DeepSeek API |
| core | `openai` | Cloud inference via OpenAI (`gpt-6-luna` by default), with streaming and native tool calling |
| core | `chatgpt` | OpenAI Responses inference using an eligible ChatGPT Plus/Pro plan and browser sign-in |
| context | `local` | Selects active context and requests compaction using a local tokenizer |
| context | `cloud` | Selects active context and requests compaction using cloud usage reports |
| memory | `continuity` | Owns the persistent transcript, summaries, named facts, history search, and memory actions in SQLite |
| memory | `remis` | Legacy semantic memory — extracts facts with a local LLM and stores embeddings in Chroma |
| action | `native` | Runs the inner model/tool loop using the core's native function-calling protocol |
| tools | `clock` | Exposes the current local date and time |
| tools | `calculator` | Safely evaluates arithmetic and common mathematical functions |
| tools | `filesystem` | Lists, reads, and searches visible files beneath a read-only root |
| tools | `web` | Searches the web and extracts readable text from public pages |

## Continuous Memory

Kayden's `continuity` memory module presents one ongoing conversation instead of
requiring the user to manage sessions. Every user and assistant message is saved to
`$XDG_DATA_HOME/kayden/continuity.sqlite3` (normally
`~/.local/share/kayden/continuity.sqlite3`) and recent messages are restored when
the app starts.

Near the model's context limit, the controller asks the selected core to summarize
older context while the memory module retains the latest messages verbatim. The
original messages remain in SQLite and can still be found through `search_history`.
The selected memory module contributes its own actions automatically; continuity
provides `remember`, `forget_memory`, `list_memories`, and `search_history`. Stable
facts are saved explicitly under named keys, so ordinary messages do
not launch a second model or embedding pipeline. Set `KAYDEN_DATA_DIR` to relocate
the continuity database. The older `remis` module remains available and its Chroma
data is not modified by the continuity module.

The filesystem tool uses the directory where Kayden was launched as its root.
Set `KAYDEN_FILESYSTEM_ROOT` to choose another directory. Hidden paths, parent
traversal, and files larger than 64 KiB are rejected. When using a cloud core,
remember that file contents returned by the tool are sent to that model.

## Writing a Module

Every module is a Python file with a class named `Module` that implements the relevant interface:

```python
from interfaces import FrontendInterface

class Module(FrontendInterface):
    def run(self, on_submit, on_interrupt):
        # Own the platform event loop and invoke these callbacks from UI events.
        ...

    def user(self, message): ...
    def assistant(self, token): ...
    def status(self, message): ...
    def finish(self, interrupted=False): ...
```

Place it under `src/modules/<capability>/your_module.py` and reference it by name in `config.toml`. Details about each interface can be found in `src/interfaces.py`.

## Available Interfaces

- `FrontendInterface` — owns the platform event loop, submits/interrupts through callbacks, and renders user, assistant, status, and completion events
- `InputInterface` — optional auxiliary input such as Whisper
- `OutputInterface` — optional auxiliary output such as Kokoro
- `CoreInterface` — `stream(messages, tools=None, should_interrupt=None) -> Iterator[CoreEvent]`, `interrupt()`
- `ContextPolicyInterface` — selects model-visible messages and owns token-budget/compaction policy, without owning storage
- `MemoryInterface` — `append`, `recall`, `view`, recent history, race-safe compaction planning/commit, reset, and optional `actions()`
- `ToolInterface` — `schemas() -> list[dict]` returns function definitions; `execute(name, arguments) -> str` validates and runs a tool
- `ActionInterface` — `run(core, turn, tools, should_interrupt=None) -> Iterator[ActionEvent]` defines how the model uses tools
- `ControllerInterface` — `run(api)`, plus `required_modules`, `optional_modules`, and `required_features` class attributes

## Actions and Tools

An action module is a strategy for using tools. It owns the inner model/tool loop: make one model request, execute requested tools through the tool runtime, return their results to the model, and continue until the model produces a final response. The built-in `native` strategy uses the core's native function-calling protocol.

A tool module under `src/modules/tools/` exposes one or more concrete tools through `schemas()` and `execute()`. A memory module may return a `ToolInterface` adapter from `actions()`; the controller adds it automatically. The shared tool runtime combines schemas, rejects duplicate or unknown names, routes execution, and converts failures into tool results. See `src/modules/tools/clock.py` for a minimal standalone example.

The controller owns the outer conversation flow and processes turns on a worker so
the frontend's event loop stays responsive. Frontends only collect user actions and
render events. Cores perform one model request at a time and emit normalized events.
The local llama.cpp core can use the `native` strategy when `tools = []`; native tool
calling is not yet supported by that core.

## Tests

```bash
python -m pip install -e ".[dev]"
pytest
```

The suite makes no live model API requests.

## Roadmap

- [ ] Model warmup
- [x] Context overflow prevention
- [x] Interrupt mid-generation
- [ ] Vision module
- [x] Native tool-use strategy
