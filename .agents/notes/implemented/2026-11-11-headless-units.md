# Agent Note: Headless units and the fleet bridge

Status: implemented — a headless frontend lets another program drive a Kadence instance over stdio; the fleet manager (KFM) is that program, a separate project not yet built

## Problem

[PHILOSOPHY.md](../../../PHILOSOPHY.md) describes a fleet of Kadence units (K1 to K5) that
work without supervision: supervisors spawn subordinates, give them work, verify it, and
escalate what they cannot resolve, up to a human at the top. A unit must be spawnable
headless, report back, keep to the budgets and permissions it is given, and be paused or
killed at any time. Communication may go through the KFM instead of directly between
units.

[Duties](2026-11-11-duties.md) gave one Kadence what a unit does by itself: a standing
instruction, a plan, wake-ups, and `ask_for_help` when it is blocked. Running one from
another program was not possible:

- **No way to drive it from outside.** The only frontends were the desktop window and the
  TUI, both for a person at a screen. Spawning `kd` as a subprocess gave the spawner no
  structured way to send a message, give a duty, answer a prompt or stop it.
- **No way to watch it from outside.** A supervisor must verify work, which means reading
  the subordinate's plan, state and, when needed, its transcript, while it runs. The
  runtime API has all of this (`snapshot`, `history`, `events`), but only in-process.
- **Questions and prompts reached nobody.** `ask_for_help` and permission prompts waited
  for the person at the screen, and a non-interactive frontend refuses every call that
  would prompt.

## Decision

Kadence has one module for this, a headless frontend; everything about fleets belongs in
the KFM, a separate program that drives units through it. Kadence stays one agent per
process and knows nothing of ranks, teams or other units (SPEC 10.2).

### The headless frontend

- `kadence.frontends.headless`, run by `kadence --headless`, which replaces the frontend
  for that run whatever is saved. It is not an installed frontend: a person never picks it
  in Settings and is left with a Kadence that ignores the keyboard.
- **Channel:** JSON-RPC 2.0, one message per line, on stdin and stdout. The framing MCP's
  stdio transport uses, so the KFM can use an existing JSON-RPC library and knows when a
  unit dies (its pipe closes). At start the frontend keeps the channel's file descriptors
  and points the process's own stdin at /dev/null and stdout at stderr, so no library
  print or subprocess can corrupt the channel.
- **Requests are RuntimeAPI's commands,** read from the protocol itself: a request names a
  command (every one but `events`, which are notifications, and `restart`), its
  parameters are decoded by the command's type hints, and it runs in its own task. The
  runtime's refusals are error -32000 with its message and the exception's class. A new
  command needs no channel code.
- **Events are notifications:** every `EventEnvelope`, with its `seq`, so a client
  recovers from a gap the way the desktop app does.
- **Encoding:** API dataclasses as JSON objects with their class name under `"type"`,
  enums as values, so a new field needs no protocol code.
- **Prompts are answered over the channel.** The frontend declares
  `interactive_permissions=True`: `PermissionRequested` goes out as an event and the
  client answers with `resolve_permission`. `HelpNeeded` goes out the same way; the answer
  is a `send`.
- **Launch options:** `--duty "…"` and `--goal "…"` start a new conversation with that
  duty, which counts as the person's action that starts a duty; a duty that cannot start
  (no instruction) is reported on stderr and the unit quits. Budget, permissions and
  model come from the unit's `config.toml`, which the spawner writes into the unit's data
  folder, so they persist with the usage they limit.
- **Quitting:** stdin closing answers what was already asked, then shuts the instance
  down as on quit (running calls cancelled or abandoned, never interrupted).

The user page is `docs/headless.md`; choices are in `.agents/decisions.md` (Headless
frontend).

### What the KFM does (a separate project, not built)

Recorded so the frontend covers it:

- **Spawning:** one process per unit, each with its own data folder and `config.toml`
  (model per rank or team, budget, permission policy clamped by the inherited ceiling).
- **Supervisor tools over MCP.** The KFM runs an MCP server per supervising unit, added
  to that unit as a tool server: `spawn_unit`, `list_units`, `read_plan`,
  `read_transcript`, `send_to_unit`, `answer_unit`, `stop_unit`. Kadence runs MCP servers
  already, so it needs nothing new. The KFM decides which tools a unit gets, which is
  where ranks are enforced (a K4 can only spawn and see K3s).
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

## Alternatives considered

- **An HTTP server per unit** (REST or WebSocket). Reachable from anywhere, but every unit
  needs a port, authentication and a way to find it, and a local unit gains nothing from
  the network. Stdio ties the unit's life to the spawner's pipe.
- **A Unix socket per unit.** No ports, but the spawner still has to find the socket and
  notice when the unit dies; a pipe gives both.
- **Units talking to each other directly,** each a server and a client. No bridge to
  build, but every unit would need fleet logic (who it may talk to, escalation, ranks),
  and the runtime would have to know other units exist. PHILOSOPHY.md suggests the bridge.
- **Several units in one process,** one runtime each. Cheaper to start, but a crash takes
  down the fleet, kill depends on cooperative cancellation, and SPEC 4.3 has one agent per
  process.
- **The KFM as a Kadence module.** One install, but the runtime would carry fleet concepts
  every single-user Kadence pays for.
- **Supervisors launching `kd` with their shell tool** (SPEC 10.1, possible already). No
  new code, but no structured channel: the supervisor cannot read a plan, answer a prompt
  or tell a stalled unit from a busy one.
- **An installed `headless` frontend,** chosen like the others. One mechanism fewer, but
  Settings would offer it to people, and choosing it leaves a Kadence that answers only
  JSON.
- **Leaving stdio as it is.** Simpler, but any library that prints, or a subprocess that
  inherits stdin, would corrupt the channel or eat its requests.

## Consequences

- A program can run Kadence units without a screen, give them work and duties, watch every
  step, answer their prompts and questions, and stop them.
- **The protocol is a contract.** The KFM depends on it, so renaming an API type or field
  changes the wire format; additions stay compatible.
- **Prompts may wait forever** if the client does not answer them. Kadence waits as it does
  for a person; the KFM must route every one.
- **Cost multiplies with ranks.** Every supervisor reading plans and transcripts spends its
  own budget. Plan first and paging keep it bounded; the KFM should start with two ranks.
- Token deltas are sent too, which a supervisor rarely needs; on a local pipe the volume
  is small.
