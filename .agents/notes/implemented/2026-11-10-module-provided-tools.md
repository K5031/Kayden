# Agent Note: Modules provide tools

Status: implemented — Memory and any number of Tools modules offer tools, routed by the runtime; MCP servers are one more Tools module

## Problem

Only two places offered tools: the one Tools module, and the runtime's built-ins (`recall`,
`remember`, `forget`, `op_status`, `op_cancel`). That caused three problems:

- **Memory logic sat in the runtime.** `remember`, `forget` and `recall` are memory
  features, but the runtime decided their wording, when two facts count as the same, and
  how `forget` finds a fact by its text. A different Memory module could change none of it,
  and the runtime had to know long-term facts exist at all.
- **Tools could not be added without code.** Adding and removing tools through MCP servers
  is common practice in agent apps (Claude, ChatGPT, Cursor, VS Code). Kayden ran exactly
  one Tools module, so an MCP module would have replaced shell, files and web instead of
  adding to them.
- **Other modules had tools to give but no way to offer them.** A Core could expose the
  provider's own server tools; a Frontend could offer tools only an interface can run.

## Decision

### Who offers tools

- **The runtime** offers only `op_status` and `op_cancel` (`api.BUILTIN_TOOLS`), which are
  about its own operations. Their names are reserved.
- **Memory** may offer tools: optional `Memory.list_tools()` and `Memory.execute(call,
  ctx)`. `kayden.memory.tools` has `recall`, `read_around` and the note tools (`read_note`, `save_note`,
  `forget_note`, see [memory as notes](2026-11-10-memory-notes.md)), built only on the
  Memory contract, with their wording and title matching; the SQLite Memory offers them. A module that cannot edit long-term memory offers `recall` and `read_around` only, and one
  that offers nothing gets no memory tools.
- **Tools modules**, any number of them, in configured order.

Core tools are offered by `Core.list_tools` ([provider web search](2026-11-10-provider-web-search.md)).
A Frontend offers whole Tools modules with `Frontend.tools()`, after every other module's
([sub-sessions](2026-11-11-sub-sessions.md)).

### The runtime routes

- Before each request the runtime lists the runtime's tools, then Memory's, then each
  Tools module's (`Runtime._offered_tools`). A name already offered is dropped from the
  later module and logged (`duplicate_tool_dropped`). Each call goes to the module that
  offered its name.
- Memory tools run like the runtime's own: never gated, outside the tool-call budget, and
  cancelled by Stop. They read and change only Kayden's memory, which the user manages in
  the Memory window. After one that is not read-only the runtime emits `MemoriesChanged`.
- A Tools module's call is judged (`confined`) and run by the module that offered it.
  Every Tools module that takes folders gets the conversation's folder; all are closed at
  shutdown.
- `ToolContext.conversation_id` names the open conversation, which `recall` and
  `remember` need.

### MCP

- `kayden.tools.mcp` is a Tools module over the official `mcp` SDK. Each server is a
  command Kayden starts or a URL, held open by a task of its own (the SDK must open and close
  a connection in the same task). Tools are named `mcp__<server>__<tool>`. No call is
  confined: under AUTO each is reviewed, under stricter levels the user is asked
  ([automatic permissions](2026-11-10-auto-permissions.md)). A command gets the SDK's safe
  environment, its own variables and the permission ceiling, never Kayden's other variables.
- Servers are settings (`api.McpServer`, `Settings.mcp_servers`): saved in settings.toml,
  their variable values in the keychain, applied at the next start like modules. The
  launcher builds the saved servers into one extra Tools module
  (`InstanceConfig.extra_tools`). The desktop app (**Kayden → Tool servers (MCP)…**) and
  the TUI (`/mcp`) add, edit and remove them, and warn that a server is someone else's
  program.

Choices and limits are listed in `.agents/decisions.md` (MCP servers).

## Alternatives considered

- **Keep tools in the runtime and move only the matching into Memory**
  (`Memory.remember` handling duplicates, a `forget_matching(text)` method). Smaller, but
  the runtime would still know long-term facts exist, and it did nothing for MCP.
- **Several Tools modules only, memory tools staying runtime built-ins.** Enough for MCP,
  but the memory logic would have stayed in the runtime.
- **MCP servers applied live,** without a restart. The tool list is read every request, so
  it is possible, but the runtime would need MCP-specific code to start and stop servers;
  as module settings they need none, and other apps (Claude Desktop) restart for them too.
- **Variables in settings.toml.** Simpler, but tokens would sit in a plain file; every
  other key Kayden keeps is in the keychain.
- **Core and Frontend tools now.** Nothing uses them yet; building them would be a
  speculative abstraction.

## Consequences

- The runtime holds no memory logic; a Memory module decides its own tools.
- Tools are added without code: a command or URL in Settings, then a restart.
- Several Tools modules complicate the runtime a little: an owner per tool name, folders
  to every module that takes one.
- An MCP server runs with the user's permissions. The settings warn, and the permission
  review or prompt covers each call, but a trusted server that misbehaves is still a risk.
- MCP servers need a restart to appear or disappear.
- Memory tools and the runtime's tools still run without permission prompts; a Memory
  module that offered a dangerous tool would not be gated.
