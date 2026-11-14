# Writing a module

Kadence is a runtime and four kinds of swappable modules. This page is for writing one. The
contract itself is `src/kadence/api.py`; it is long, so start here and open it for the
details of the part you need.

| Kind | It provides | You must write | Example to copy | Effort |
|---|---|---|---|---|
| **Tools** | Actions the agent can take | `list_tools`, `execute` | [`examples/dice_tools.py`](../examples/dice_tools.py) | Small |
| **Core** | A model provider | `list_models`, `stream` | [`examples/echo_core.py`](../examples/echo_core.py) | Medium |
| **Frontend** | The user interface | `info`, `run` | [`examples/line_frontend.py`](../examples/line_frontend.py) | Medium to large |
| **Memory** | Where conversations are kept and what the model sees of them | Eight methods | [`examples/ram_memory.py`](../examples/ram_memory.py) | Large |

The examples are complete and tested (`tests/test_examples.py`). To see all four run
together, from a checkout:

```toml
# /tmp/kadence-examples/config.toml
[core]
factory = "examples.echo_core:factory"
[memory]
factory = "examples.ram_memory:factory"
[tools]
factory = "examples.dice_tools:factory"
[frontend]
factory = "examples.line_frontend:factory"
```

```bash
PYTHONPATH=. uv run kadence --data-dir /tmp/kadence-examples
```

Type a line and the echo "model" repeats it, with the time the runtime stamps on every
message; `/quit` leaves.

## What every module has

- **A factory**: `factory(options, paths)` returns the module. `options` is the module's
  table in `config.toml`; `paths.data_dir` is the folder an instance keeps its files in.
- **A way to be found.** Name it in `config.toml` by import path
  (`factory = "my_package.my_module:factory"`), or declare an entry point so it shows up in
  the settings; see [Registering it](#registering-it).
- **Async methods that never block.** Everything runs on one event loop. Put file reads,
  subprocesses and anything slow in `asyncio.to_thread` or use async libraries.
- **No knowledge of other modules.** A module imports `kadence.api` and nothing else from
  Kadence. The runtime carries everything between modules.
- **Errors are safe.** The runtime wraps every call into a module: an exception becomes a
  failure the user sees, never a crash. Stop cancels the running call with ordinary
  asyncio cancellation; clean up and let it propagate.

## Tools

The agent sees each `ToolSpec` you return from `list_tools` (a name, a description, a JSON
Schema for the arguments) and may call it. The runtime checks the arguments against the
schema, asks for permission where the user's settings require it, then calls `execute`.

- **Return, do not raise.** An expected failure is `ToolOutput("why", is_error=True)`.
- **`read_only=True`** on a spec lets several calls run at once and lets stricter
  permission levels run it without asking.
- **Long calls** should call `ctx.progress("…")` now and then. A call that takes long is
  moved to the background by the runtime; you do nothing for that.
- **Pictures and PDFs**: `ToolOutput(text, attachments=(Image(...),))` shows them to the
  model with the text.

Optional, each a method you may leave out:

| Method | Implement it to |
|---|---|
| `use_folder(folder)` | Work in the folder of the open conversation |
| `confined(call)` | Say that a call cannot touch anything outside that folder, so it runs without asking. `execute` gets your answer as `ctx.confined` |
| `attach(path)` | Read a file the user dropped into a message |
| `aclose()` | Release what you hold when Kadence quits |

## Core

`stream(request)` is an async generator. The request has the model id, the system prompt,
the messages and the tools on offer. Yield `TextDelta` (and `ReasoningDelta`,
`ToolCallDelta`) as the answer arrives, `UsageReport` when the provider tells you token
counts, and always end with `Completed`: the whole assistant message, the usage and why
it stopped.

- **Messages are blocks.** Turn each into your provider's format: `Text`, `ToolCall`,
  `ToolResult`, and what a user attached (`Image`, `Document`, `File`).
  `attachment_text(block)` gives any attachment as text when your provider cannot take it.
- **Errors**: raise `TransientCoreError` for what is worth retrying (rate limits,
  timeouts, 5xx; the runtime retries, you do not), `ContextOverflowError` when the prompt
  is too long, `CoreError` for the rest.
- **Prices**: give each `ModelInfo` its prices per million tokens, or report the cost in
  `Usage.cost_usd`, so that a spending limit can work.
- **An API key**: set `factory.key_name = "MY_API_KEY"` and the settings ask for it and
  keep it in the keychain.

Optional: `list_tools` (tools your provider runs itself, such as its web search),
`count_tokens` (an exact count, used to calibrate estimates), `aclose`.

## Frontend

`run(runtime)` is your whole program; when it returns, Kadence quits. You get a
`RuntimeAPI`:

- **Commands**: `start` a conversation, `send` a message, `stop`, `resolve_permission`,
  and about two dozen more for conversations, settings, memory and duties. Use the ones
  your interface offers.
- **Events**: `runtime.events()` yields everything that happens, each with a sequence
  number. A small interface needs `MessageCommitted` (a finished message), `StateChanged`
  (working or idle), `PermissionRequested` (answer it, or the agent waits) and `Failure`.
  `TextDelta` and `ToolStarted` make it feel live.
- **Catching up**: `runtime.snapshot()` plus `runtime.history(...)` rebuild the screen;
  a gap in the sequence numbers means you missed events and should do that.

`src/kadence/frontends/common.py` has what the built-in apps share: how a tool step reads,
permission texts, the plan checklist. Reuse it rather than rewording.

Set `factory.kind = "window"` for a graphical frontend, and `factory.event_loop` when your
toolkit needs its own loop (the desktop app names Qt's).

## Memory

Memory stores every message and decides what the model sees of a conversation.

| Method | Does |
|---|---|
| `create_conversation`, `list_conversations`, `update_conversation` | Keep the list of conversations and their titles |
| `append` | Store messages for good; the same message twice is a no-op |
| `history` | Return the full transcript, optionally a page of it |
| `assemble` | Return what fits the model's context: a prompt addendum (summary, long-term memory) and the recent messages |
| `maintain` | Summarize older messages when the conversation grows, using the model call it is given; return whether it did anything |
| `search` | Find messages by plain text |

The example keeps everything in RAM and never summarizes, which is enough to see the
shape. A real one must survive a restart, keep `assemble` inside its token budget and
never cut a tool call from its result. **The practical way is to build on
`kadence.memory.sqlite.SqliteArchive`**, which does all of that, and add your own
long-term memory through its three hooks; `kadence.memory.files` is under 400 lines on top
of it.

Optional: tools of your own (`list_tools`, `execute`), notes the user can edit (`notes`,
`save_note`, `delete_note`), `save_duty`, `delete_conversation`, `inspect`, `aclose`.

## Registering it

Declare the factory as an entry point in your package's `pyproject.toml`, in the group for
its kind. The name is the module's id; once the package is installed in Kadence's
environment, the module is listed in the settings:

```toml
[project.entry-points."kadence.tools"]   # or kadence.cores, kadence.memories, kadence.frontends
dice = "my_package.dice:factory"
```

`factory.display_name` is shown instead of the id. A module is a program that runs with
the user's permissions, so say plainly what yours does.

## Checking it

The conformance suites in `tests/conformance/` check a module against the contract:
`tools_suite.py`, `core_suite.py` and `memory_suite.py`. They are part of Kadence's tests,
so run them from a Kadence checkout with your package installed in it. Each is a class to
subclass with a fixture that builds your module; the top of each file shows how:

```python
from tests.conformance.memory_suite import MemoryConformance

class TestMyMemory(MemoryConformance):
    @pytest.fixture
    def memory_factory(self, tmp_path):
        return lambda: MyMemory(tmp_path / "store")
```

The built-in modules pass them, and so should a module meant for others to use.

## Good to know

- **The contract is a draft.** `api.py` still changes as Kadence grows, and there is no
  version check for modules yet: pin the Kadence version you built against.
- **The top of `api.py`** lists what the runtime guarantees a module: the order things
  are stored in, what happens after a crash, when `maintain` is called.
- **Skills are not modules.** For instructions the agent should follow for one kind of
  task, write a [skill](concepts.md#skills): a folder with a text file, no code.
