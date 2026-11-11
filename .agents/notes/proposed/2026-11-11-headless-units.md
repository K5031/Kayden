# Agent Note: Headless units and the fleet bridge

Status: proposed — a headless frontend lets another program drive a Kayden instance over stdio; the fleet manager (KFM) is that program, a separate project

## Problem

[PHILOSOPHY.md](../../../PHILOSOPHY.md) describes a fleet of Kayden units (K1 to K5) that
work without supervision: supervisors spawn subordinates, give them work, verify it, and
escalate what they cannot resolve, up to a human at the top. A unit must be spawnable
headless, report back, keep to the budgets and permissions it is given, and be paused or
killed at any time. Communication may go through the KFM instead of directly between
units.

[Duties](../implemented/2026-11-11-duties.md) gave one Kayden what a unit does by itself:
a standing instruction, a plan, wake-ups, and `ask_for_help` when it is blocked. What is
missing is everything another program needs to run one:

- **No way to drive it from outside.** The only frontends are the desktop window and the
  TUI, both for a person at a screen. Spawning `kd` as a subprocess gives the spawner no
  structured way to send a message, give a duty, answer a prompt or stop it.
- **No way to watch it from outside.** A supervisor must verify work, which means reading
  the subordinate's plan, state and, when needed, its transcript, while it runs. The
  runtime API has all of this (`snapshot`, `history`, `events`), but only in-process.
- **Questions and prompts reach nobody.** `ask_for_help` and permission prompts wait for
  the person at the screen. A headless unit has none: today a non-interactive frontend
  refuses every call that would prompt.

## Proposal

Kayden gains one module, a headless frontend; everything about fleets lives in the KFM, a
separate program that drives units through it. Kayden stays one agent per process and
knows nothing of ranks, teams or other units (SPEC 10.2).

### The headless frontend

- `kayden.frontends.headless`, an ordinary Frontend module (entry point `headless`),
  started with `kd --frontend headless` or chosen in a unit's `config.toml`.
- **Channel:** JSON-RPC 2.0, one message per line, on stdin and stdout. Logs go to stderr.
  The same framing MCP's stdio transport uses, so the KFM can use an existing JSON-RPC
  library and knows when a unit dies (its pipe closes).
- **Requests map one to one to `RuntimeAPI`:** `start`, `send`, `activate`, `stop`,
  `cancel_operation`, `select_model`, `set_duty`, `set_plan`, `resolve_permission`,
  `snapshot`, `history`, `list_conversations`, `settings`. Results and errors are the
  runtime's, with `RuntimeStateError` and `ValueError` as JSON-RPC errors that keep their
  message.
- **Events are notifications:** every `EventEnvelope` the runtime emits, with its `seq`,
  so the KFM recovers from a gap the way the desktop app does (snapshot, drop older
  envelopes, history). Deltas can be switched off per connection; a supervisor rarely
  needs token streaming.
- **Encoding:** API dataclasses as JSON objects with a type tag, reusing the block codec
  for messages, so new fields need no new protocol code.
- **Prompts are answered by the channel.** The frontend declares
  `interactive_permissions=True`: `PermissionRequested` goes out as an event and the KFM
  answers with `resolve_permission`. `HelpNeeded` goes out the same way; the answer is a
  `send`.
- **Launch options:** `--duty "…"` and `--goal "…"` start a new conversation with that duty
  on start-up, which counts as the person's action that starts a duty. Budget, permissions
  and model come from the unit's `config.toml`, which the spawner writes into the unit's
  data folder, so they persist with the usage they limit.
- **Quitting:** `stdin` closing ends `run()` and shuts the instance down as on quit
  (running calls cancelled or abandoned, never interrupted).

### What the KFM does (a separate project)

Listed so the frontend covers it; none of it is built in Kayden.

- **Spawning:** one process per unit, each with its own data folder and `config.toml`
  (model per rank or team, budget, permission policy clamped by the inherited ceiling).
- **Supervisor tools over MCP.** The KFM runs an MCP server per supervising unit, added
  to that unit as a tool server: `spawn_unit`, `list_units`, `read_plan`,
  `read_transcript`, `send_to_unit`, `answer_unit`, `stop_unit`. Kayden needs nothing new
  for this: it already runs MCP servers as a Tools module. The KFM decides which tools a
  unit gets, which is where ranks are enforced (a K4 can only spawn and see K3s).
- **Watching, by three rules:**
  - **Read-only.** Reading a unit never changes what it does; changing it is a message,
    which the unit sees like any other.
  - **Plan first, transcript on demand.** `read_plan` is cheap and comes first;
    `read_transcript` costs the supervisor its own context and money, so it pages and is
    asked for only when the plan is not enough.
  - **Scoped by rank.** A supervisor sees the units below it; a human sees every unit.
- **Escalation.** A unit's `HelpNeeded` and permission prompts go to its supervisor, which
  answers or escalates one rank up, to the human only at the top. Actions with real-world
  effect (publishing, payments, outbound messages) go to the human unless the human
  granted them explicitly.
- **Failure handling.** A unit that dies, stalls or fails is respawned, reassigned or
  escalated by its supervisor; the KFM tells the supervisor through its MCP server.
- **Pause and kill.** Pause is `stop`. Kill is `stop`, then closing stdin, then the
  process signal if it does not exit within the grace period.

### Spec changes

- SPEC 3 lists a headless frontend driven over stdio by another program.
- SPEC 10.1 and 10.2: instances may be driven through the headless frontend; the fleet
  manager is a separate program (contract issue D1).

## Alternatives considered

- **An HTTP server per unit** (REST or WebSocket). Reachable from anywhere, but every unit
  needs a port, authentication and a way to find it, and a local unit gains nothing from
  the network. Stdio ties the unit's life to the spawner's pipe.
- **A Unix socket per unit.** No ports, but the spawner still has to find the socket and
  notice when the unit dies; a pipe gives both for free.
- **Units talking to each other directly,** each a server and a client. No bridge to
  build, but every unit would need fleet logic (who it may talk to, escalation, ranks),
  and the runtime would have to know other units exist. PHILOSOPHY.md suggests the bridge.
- **Several units in one process,** one runtime each. Cheaper to start, but a crash takes
  down the fleet, kill depends on cooperative cancellation, and SPEC 4.3 has one agent per
  process.
- **The KFM as a Kayden module.** One install, but the runtime would carry fleet concepts
  every single-user Kayden pays for.
- **Supervisors launching `kd` with their shell tool** (SPEC 10.1, possible today). No new
  code, but no structured channel: the supervisor cannot read a plan, answer a prompt or
  tell a stalled unit from a busy one.

## Acceptance criteria

- `kd --frontend headless --duty "…"` starts a unit that works on its duty with no
  terminal or display, and shuts down cleanly when stdin closes.
- Every request above round-trips over the channel, and errors keep their message.
- A client that subscribes, reads `snapshot` and `history` and then follows events sees
  exactly what the desktop app would, gaps included.
- A permission prompt and an `ask_for_help` question reach the client, and its answer
  resumes the unit.
- The protocol is documented in `docs/` with an example session.

## Risks

- **The protocol becomes a contract.** Once the KFM depends on it, changing an API type
  changes the wire format. The type-tagged encoding keeps additions compatible; renames
  are breaking.
- **Prompts may wait forever** if the KFM does not answer them. The KFM must route every
  one; Kayden only waits, as it does for a person.
- **Cost multiplies with ranks.** Every supervisor reading plans and transcripts spends
  its own budget. Plan first and paging keep it bounded; the KFM should start with two
  ranks.
