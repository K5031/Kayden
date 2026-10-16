# Kayden

A modular desktop AI assistant. Its `k` command runs a controller that connects
input, output, model, context, memory, action-strategy, and tool modules.

## Installation

```bash
git clone https://github.com/K5031/Kayden.git
cd Kayden
python3 -m venv venv
source venv/bin/activate
python -m pip install -e .
```

The base installation supports the terminal and DeepSeek modules. Install only the optional features you need:

```bash
python -m pip install -e ".[local]"         # local llama.cpp core
python -m pip install -e ".[memory]"        # persistent Remis memory
python -m pip install -e ".[voice-input]"   # Whisper microphone input
python -m pip install -e ".[voice-output]"  # Kokoro speech output
python -m pip install -e ".[all]"           # everything
```

If you'll use a local core or memory module, drop the GGUF models you want into `models/` when running from a checkout. A regular installation uses `$XDG_DATA_HOME/k/models/` (normally `~/.local/share/k/models/`).

Run it:

```bash
k
# Or, from a source checkout: python src/main.py
```

On first run this creates `~/.config/k/config.toml` and `~/.config/k/system_prompt.txt` from defaults bundled with the package. Edit `config.toml` to pick your modules and `system_prompt.txt` to customize the assistant's personality.

Some modules need credentials — e.g. the `deepseek` core module reads its key from the `DEEPSEEK_API_KEY` environment variable.

## Configuration

The default configuration uses DeepSeek, terminal input/output, no persistent
memory, and the clock tool:

```toml
controller = "cloud"
input = "terminal"
output = "terminal"
core = "deepseek"
context = "cloud"
memory = "none"
action = "native"
tools = ["clock"]
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

Each key is a capability, and its value is the module (filename, no `.py`) that fills it, loaded from `src/modules/<capability>/`. The `controller` module must be listed first — it declares which capabilities it needs and which are optional. `Registry.from_config` checks the config against those declarations.

The `output` and `tools` capabilities can take lists. For example, `output =
["terminal", "kokoro"]` runs several outputs side by side. Every module named
in `tools` contributes its concrete tools to the selected action strategy.
Other capabilities take one module.

## Built-in Modules

| Capability | Module | Description |
|---|---|---|
| controller | `local` | Interactive loop; interrupts generation on new input or Ctrl+C |
| controller | `cloud` | Interactive loop; also feeds reported token usage back into context trimming |
| input | `terminal` | Text input via stdin |
| input | `whisper` | Microphone capture with local Whisper speech-to-text |
| output | `terminal` | Prints tokens to stdout |
| output | `kokoro` | Streams sentence-buffered text through Kokoro TTS |
| core | `llama_cpp` | Local inference via llama.cpp, using a GGUF model from `models/` |
| core | `deepseek` | Cloud inference via the DeepSeek API |
| context | `local` | In-memory conversation buffer, trimmed by counting tokens with a local tokenizer |
| context | `cloud` | In-memory conversation buffer, trimmed using usage stats reported by the core module |
| memory | `none` | Disables persistent memory without adding heavy dependencies |
| memory | `remis` | Persistent long-term memory — extracts durable facts with a local LLM, then embeds, dedupes, and conflict-resolves them against a Chroma store |
| action | `native` | Runs the inner model/tool loop using the core's native function-calling protocol |
| tools | `clock` | Exposes the current local date and time |

## Writing a Module

Every module is a Python file with a class named `Module` that implements the relevant interface:

```python
from interfaces import InputInterface

class Module(InputInterface):
    def get_input(self) -> str:
        return input("> ")

    def has_input(self) -> bool:
        return False
```

Place it under `src/modules/<capability>/your_module.py` and reference it by name in `config.toml`. Details about each interface can be found in `src/interfaces.py`.

## Available Interfaces

- `InputInterface` — `get_input() -> str`, `has_input() -> bool`
- `OutputInterface` — `send(token: str)`, `interrupt()`, `flush()`
- `CoreInterface` — `stream(messages, tools=None, should_interrupt=None) -> Iterator[CoreEvent]`, `interrupt()`
- `ContextInterface` — `add(role, content)`, `get() -> list[dict]`, `clear()`
- `MemoryInterface` — `store(messages)`, `retrieve(query) -> str`
- `ToolInterface` — `schemas() -> list[dict]` returns function definitions; `execute(name, arguments) -> str` validates and runs a tool
- `ActionInterface` — `run(core, turn, tools, should_interrupt=None) -> Iterator[ActionEvent]` defines how the model uses tools
- `ControllerInterface` — `run(api)`, plus `required_modules`, `optional_modules`, and `required_features` class attributes

## Actions and Tools

An action module is a strategy for using tools. It owns the inner model/tool loop: make one model request, execute requested tools through the tool runtime, return their results to the model, and continue until the model produces a final response. The built-in `native` strategy uses the core's native function-calling protocol.

A tool module under `src/modules/tools/` exposes one or more concrete tools through `schemas()` and `execute()`. The shared tool runtime combines schemas, rejects duplicate or unknown names, routes execution, and converts failures into tool results. See `src/modules/tools/clock.py` for a minimal example.

The controller still owns the outer conversation loop, context, memory, and output. Cores perform one model request at a time and emit normalized events. The local llama.cpp core can use the `native` strategy when `tools = []`; native tool calling is not yet supported by that core.

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The suite makes no live model API requests.

## Roadmap

- [ ] Model warmup
- [x] Context overflow prevention
- [x] Interrupt mid-generation
- [ ] Vision module
- [x] Native tool-use strategy
