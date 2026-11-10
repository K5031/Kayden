# Agent Note: Modules provide tools

Status: proposed — any module may offer tools, routed by the runtime; build together with MCP

## Problem

Only two places offer tools today: the one Tools module, and the runtime's built-ins
(`recall`, `remember`, `forget`, `op_status`, `op_cancel`, in `api.BUILTIN_TOOLS`). That
causes three problems:

- **Memory logic sits in the runtime.** `remember`, `forget` and `recall` are memory
  features, but the runtime decides their wording, when two facts count as the same, and
  how `forget` finds a fact by its text. A different Memory module cannot change any of
  that, and the runtime knows long-term facts exist at all.
- **Other modules have tools to give but no way to offer them.** A Core could expose the
  provider's own server tools (Anthropic's and OpenAI's web search and code execution;
  see [web search](../implemented/2026-11-10-web-search.md) for why Kayden uses its own). A
  Frontend could offer tools only an interface can run: ask the user a question with
  buttons, show an image, open a file in the viewer.
- **Tools cannot be added without code.** Adding and removing tools through MCP servers is
  common practice in agent apps today (Claude, ChatGPT, Cursor, VS Code). Kayden runs exactly
  one Tools module, so an MCP module would replace shell, files and web instead of adding to
  them.

## Proposal

### Any module may offer tools

An optional pair on every module contract (Core, Memory, Tools, Frontend), the same shape
the Tools contract has now:

- `list_tools() -> Sequence[ToolSpec]`: called once per request, as `Tools.list_tools`
  is today. Default: none.
- `execute(call, ctx) -> ToolOutput`: runs one of the module's own tools.
- `confined(call)` stays available to any provider; the default False means the call is
  reviewed or asks, like any other.

Several Tools modules may run at once (shell-files and mcp, for example), each one a provider.

### The runtime routes

- Before each request the runtime collects every provider's tools, adds the step sentence
  argument (`api.DESCRIPTION_ARG`, [step descriptions](../implemented/2026-11-10-step-descriptions.md)) and remembers which provider offered each name.
- A call goes to the provider that offered its name. Validation, permissions, budgets,
  backgrounding, Stop, events and the transcript stay in the runtime, the same for every
  provider. Modules still never call each other.
- Name clashes: the first provider in a fixed order (runtime, Memory, Tools modules in
  configured order, Core, Frontend) keeps the name; later ones are dropped and logged, as
  reserved names are today.
- `PermissionRequest` and the step display name the provider, so the user sees which
  module (or MCP server) wants what.

### What moves

- `recall`, `remember` and `forget` leave `api.BUILTIN_TOOLS` for the SQLite Memory
  module, with their wording, duplicate check and text matching. The runtime keeps only
  `op_status` and `op_cancel`, which are about its own operations.
- A Memory module without long-term facts simply offers no `remember` tool, replacing the
  `_can_edit_memories` probe.
- The Memory window keeps using the `RuntimeAPI` memory commands; moving those out of the
  runtime is a separate decision (a module offering its own settings or panel).

### MCP

An `mcp` Tools module built on the official `mcp` Python SDK: servers (a command, or a URL)
added and removed in Settings, applied live, since the tool list is read every request.
MCP tools are never confined: under AUTO each call is reviewed
([automatic permissions](../implemented/2026-11-10-auto-permissions.md)), under stricter levels the
user is asked. Settings says that a server is someone else's program running with the
user's permissions. Its own design note follows when it is built.

## Alternatives considered

- **Keep tools in the runtime and move only the matching into Memory** (`Memory.remember`
  handles duplicates, a `forget_matching(text)` method). Smaller and needs no new mechanism,
  but the runtime still knows long-term facts exist, and it does nothing for Core or
  Frontend tools or for MCP.
- **Several Tools modules only, no tools from other module types.** Enough for MCP, but
  memory tools stay runtime built-ins and provider server tools stay out of reach.
- **Keep everything as it is.** No contract change, but tools cannot be added without code,
  which users expect.

## Acceptance criteria

- With the SQLite Memory, the agent remembers, forgets and recalls as today; the runtime
  source no longer mentions long-term memory outside the `RuntimeAPI` memory commands.
- A Memory module that offers no tools runs without `remember`, `forget` and `recall`.
- Two Tools modules run together; each call reaches the module that offered it; a name
  clash keeps the first and logs the second.
- An MCP server added in Settings shows its tools on the next request and loses them when
  removed, without a restart.
- Permission prompts and steps name the providing module or server.
- Spec section 16, `api.py` and the docs describe the mechanism.

## Risks

- **A bigger contract.** Every module type gets an optional second job; module authors
  must understand tools even when they offer none (the defaults keep that cost at zero).
- **Unclear ownership.** A tool could fit two modules; the clash order decides, which may
  surprise.
- **Permissions per provider.** The runtime treats every unconfined call the same; MCP
  servers may need per-server trust settings later.
- **Server tools from a Core** run on the provider's side, mid-stream: they may need new
  block types and do not fit execute() cleanly. That part may stay out of the first build.
