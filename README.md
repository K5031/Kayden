# Kayden

> Weapon of the Faithless.

A modular desktop AI assistant. Its fixed application loop connects a selectable
frontend, model, context, memory, action strategy, and tool modules.

## Installation

```bash
python -m pip install kayden
kayden
```

To work from a source checkout instead:

```bash
git clone https://github.com/K5031/Kayden.git
cd Kayden
python3 -m venv venv
source venv/bin/activate
python -m pip install -e ".[dev]"
```

If this checkout was installed before the distribution was renamed from `k`,
remove that stale editable installation once before reinstalling:

```bash
python -m pip uninstall k
python -m pip install -e ".[dev]"
```

The base installation supports the TUI, DeepSeek, OpenAI API, and ChatGPT plan
modules. The self-contained Tauri frontend module is currently built from the
source checkout.
Install only the other optional features you need:

```bash
python -m pip install "kayden[local]"         # local llama.cpp core
python -m pip install "kayden[memory]"        # legacy Remis memory dependencies
python -m pip install "kayden[voice-dictation]"  # push-to-talk with Faster Whisper
python -m pip install "kayden[voice-input]"      # always-listening OpenAI Whisper
python -m pip install "kayden[voice-output]"     # Kokoro speech output
python -m pip install "kayden[all]"              # everything
```

When working from a checkout, append the same extra to the editable install—for
example, `python -m pip install -e ".[local,dev]"`.

If you'll use a local core or memory module, drop the GGUF models you want into
`models/` when running from a checkout. A regular installation uses
`$XDG_DATA_HOME/kayden/models/` (normally `~/.local/share/kayden/models/`).

Run it:

```bash
kayden
# Or: python -m kayden
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
frontend = "tui"
core = "deepseek"
context = "cloud"
memory = "continuity"
action = "native"
tools = ["clock", "calculator", "filesystem", "web"]

[assistant]
name = "Kayden"
```

`assistant.name` is the user-facing identity shown by every frontend. Changing
it updates the window title, logo, assistant labels, and message composer; it
does not rename the Kayden application or rewrite the system prompt.

For fully local inference, install the `local` extra and switch the relevant
settings:

```toml
core = "llama_cpp"
context = "local"
tools = []
```

The local core does not support native tool calling yet.

To use OpenAI instead, keep `context = "cloud"` and set:

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
request a specific available model. The Tauri frontend also turns its model badge
into a selector when the selected core advertises multiple models; a selection
applies to subsequent responses for the current run. This connection does not give Kayden access
to ChatGPT conversations or memories. Disconnect the app in ChatGPT settings if
the local credentials are ever exposed.

Choose the user experience independently from the rest of Kayden:

```toml
frontend = "tui"    # full-screen terminal interface
frontend = "tauri"  # React/TypeScript desktop interface
frontend = "qt"     # native PySide6/QML desktop interface
```

The Qt frontend is the pip-only desktop option. Install its module dependency,
then select it in the config:

```bash
pip install "kayden[qt]"
```

```toml
frontend = "qt"

[settings.frontend.qt]
width = 1000
height = 760
```

It supports streamed responses, tool and error activity, interruption, push-to-talk
controls, assistant identity, transcript restoration, and runtime model selection.

The desktop module opens in its full layout by default. For its compact
always-on-top layout, add module-owned settings:

```toml
[settings.frontend.tauri]
mode = "floaty"
```

The `tauri` module owns its React/TypeScript client, Rust shell, Python bridge,
metadata, and tests as one removable unit. Kayden's runtime remains responsible
for models, memory, actions, and tools; the desktop client connects through an
authenticated local bridge bound only to `127.0.0.1`.

To run the desktop client from a checkout, install Node.js, Rust, and
[Tauri's platform prerequisites](https://v2.tauri.app/start/prerequisites/),
then prepare the frontend once:

```bash
cd src/kayden/modules/frontend/tauri/desktop
npm install
cd ../../../../../..
```

With `frontend = "tauri"`, `kayden` launches the Tauri development client
automatically. `KAYDEN_TAURI_MODE` can override the configured mode for a
single launch. A prebuilt client can be selected with
`KAYDEN_DESKTOP_BINARY=/path/to/kayden-desktop`. The future module marketplace
can install or remove the complete `tauri` directory and its platform binary
without changing Kayden's runtime or TUI.

### Voice

Voice remains optional. Install either or both extras, then add the modules you
want to `~/.config/kayden/config.toml`:

```toml
input = "dictation"  # push-to-talk with Faster Whisper + Silero VAD
output = "kokoro"    # speaks assistant replies
```

With `dictation`, press the GUI's **Mic** button or `Ctrl-Y` in the TUI to
start recording, then use the same control to stop. Kayden inserts the
transcript into the message composer so you can correct it before sending.
Press Escape while recording in the TUI to discard the clip. The transcription
model is loaded lazily on the first completed recording.

The original `input = "whisper"` module remains available for hands-free,
always-listening input. Install `voice-dictation` for `dictation`, or
`voice-input` for `whisper`.

Check what Kayden can actually access before launching it:

```bash
kayden doctor
```

The command lists the microphone and output devices visible to Kayden. If the
desired device is not selected automatically, set `KAYDEN_INPUT_DEVICE` or
`KAYDEN_OUTPUT_DEVICE` to either its numeric index or part of its displayed
name. Whisper now defaults to the much smaller `base.en` model; override it with
`KAYDEN_WHISPER_MODEL`. Dictation defaults to `small.en`; override it with
`KAYDEN_DICTATION_MODEL`, `KAYDEN_DICTATION_DEVICE`, and
`KAYDEN_DICTATION_COMPUTE_TYPE`. Dictation uses portable CPU `int8` inference
by default; opt into an installed CUDA runtime with
`KAYDEN_DICTATION_DEVICE=cuda`. Kokoro can be adjusted with
`KAYDEN_KOKORO_VOICE`, `KAYDEN_KOKORO_LANGUAGE`, and `KAYDEN_KOKORO_DEVICE`.

Whisper submits a phrase after about 2 seconds of silence. This keeps voice
input conversational while still allowing brief pauses within a sentence.

The first Whisper or Kokoro launch may download model data, so it needs network
access. On Linux the process must also be able to reach PipeWire/PulseAudio (or
ALSA) and its microphone permission must be enabled. A sandbox or container
without `/dev/snd` and the desktop audio socket cannot provide voice.

Each top-level capability key selects a module, loaded from
`kayden.modules.<capability>`. A module may be either one file or a directory
that owns all of its implementation and assets. Multi-file modules put their
interface implementation and `Module` entrypoint in `main.py`; their
`__init__.py` only re-exports that class. All other internal filenames and
structure remain module-owned. The optional
`settings.<capability>.<module>` table is passed only to that selected module.
Kayden requires one
frontend, core, context, memory, and action module plus a tools list. Input and
output are optional. Configuration order does not matter, and
`Registry.from_config` validates the selected modules before starting them.

Every module named in `tools` contributes its concrete tools to the selected action
strategy. Other main capabilities take one module. The `input` and `output`
capabilities are optional auxiliaries for voice: for example, add `input = "whisper"`
or `output = "kokoro"` alongside any frontend. Old terminal input/output configs are
automatically normalized to `frontend = "tui"` when loaded, and obsolete
`controller` settings are ignored.

## Built-in Modules

| Capability | Module | Description |
|---|---|---|
| frontend | `tui` | Full-screen terminal chat with streaming, scrolling, and interruption |
| frontend | `tauri` | React/Tauri desktop chat with full and compact floating layouts |
| frontend | `qt` | Native PySide6/QML desktop chat installable entirely through pip |
| input | `dictation` | Push-to-talk Faster Whisper input with VAD and editable transcripts |
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

Near the model's context limit, the application asks the selected core to summarize
older context while the memory module retains the latest complete turns verbatim.
The cloud policy compacts by token budget rather than message count: for its default
60k working context it targets roughly 35k tokens of recent conversation, a summary
below 4k, up to 2k for durable memories, and 19k free after compaction. Compaction
starts at a lower 10k free-space watermark so it does not run after every exchange.
These are budgets rather than separate storage limits. The original messages remain
in SQLite and can still be found through `search_history`.
The selected memory module contributes its own actions automatically; continuity
provides `remember`, `forget_memory`, `list_memories`, and `search_history`. Stable
facts are saved explicitly under named keys. Each key owns one atomic value—for
example, `profile.university = UCL` and
`profile.degree_subject = Computer Science`—so related facts do not duplicate
one another. Ordinary messages do
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
from kayden.interfaces import FrontendInterface

class Module(FrontendInterface):
    def run(self, on_submit, on_interrupt):
        # Own the platform event loop and invoke these callbacks from UI events.
        ...

    def user(self, message): ...
    def assistant(self, token): ...
    def status(self, message): ...
    def finish(self, interrupted=False): ...
```

Place it under `src/kayden/modules/<capability>/your_module.py` and reference it
by name in `config.toml`. Details about each interface can be found in
`src/kayden/interfaces.py`.

## Available Interfaces

- `FrontendInterface` — owns the platform event loop, submits/interrupts through callbacks, and renders user, assistant, status, and completion events
- `InputInterface` — optional streaming auxiliary input such as always-listening Whisper
- `ManualInputInterface` — explicit start/finish/cancel capture that returns an editable frontend draft
- `OutputInterface` — optional auxiliary output such as Kokoro
- `CoreInterface` — `stream(messages, tools=None, should_interrupt=None) -> Iterator[CoreEvent]`, `interrupt()`
- `ContextPolicyInterface` — selects model-visible messages and owns token-budget/compaction policy, without owning storage
- `MemoryInterface` — `append`, `recall`, `view`, recent history, race-safe compaction planning/commit, reset, and optional `actions()`
- `ToolInterface` — `schemas() -> list[dict]` returns function definitions; `execute(name, arguments) -> str` validates and runs a tool
- `ActionInterface` — `run(core, turn, tools, should_interrupt=None) -> Iterator[ActionEvent]` defines how the model uses tools

## Actions and Tools

An action module is a strategy for using tools. It owns the inner model/tool loop: make one model request, execute requested tools through the tool runtime, return their results to the model, and continue until the model produces a final response. The built-in `native` strategy uses the core's native function-calling protocol.

A tool module under `src/kayden/modules/tools/` exposes one or more concrete tools through `schemas()` and `execute()`. A memory module may return a `ToolInterface` adapter from `actions()`; the application adds it automatically. The shared tool runtime combines schemas, rejects duplicate or unknown names, routes execution, and converts failures into tool results. See `src/kayden/modules/tools/clock.py` for a minimal standalone example.

Kayden's fixed application loop owns the outer conversation flow and processes
turns on a worker so the frontend's event loop stays responsive. Frontends only
collect user actions and render events. Cores perform one model request at a time
and emit normalized events.
The fixed application, registry, and tool-routing machinery live under
`src/kayden/runtime/`; this is internal infrastructure, not another configurable
module category.
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
