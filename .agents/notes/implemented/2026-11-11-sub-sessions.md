# Agent Note: Sub-sessions

Status: implemented — the frontend hosts sub-sessions: it starts headless Kayden children for the agent, shows them, and lets the user watch their work

## Problem

The owner uses an app (the Claude desktop app) where an agent hands self-contained tasks to
sub-sessions. They work on their own, report back, and the user can open any of them to see
what it is doing. Kayden could not do this. An agent can only work in its one conversation; a
long side task fills its context and blocks the conversation while it runs.

[Headless units](2026-11-11-headless-units.md) made the child easy: a
Kayden started with `--headless` takes a goal and reports when it is done. What is
missing is the host. In the Claude app the app itself starts and holds every session, so
listing and peeking are trivial. In Kayden the user-facing host is the frontend.

## Decision

### Frontends may offer tools

- `Frontend.tools()`, optional (default none): Tools modules the frontend runs, offered
  after every other module's. `run_instance` builds the frontend before the runtime, so it
  passes them into the runtime's Tools list. They are ordinary Tools modules: judged,
  gated, counted and closed like any other ([module-provided
  tools](2026-11-10-module-provided-tools.md)).

### The sub-session manager

`kayden.frontends.sessions.SubSessions`, a Tools module with no UI code, which the
desktop app and the TUI create and offer. It offers:

- `start_session(task, budget_usd)`: starts a child, a separate Kayden agent with its own
  context, in the conversation's work folder, on the task as a goal. The call runs in the
  background and returns the child's report when the goal is reached, or its question when
  it asks for help.
- `message_session(session, text)`: answers a question or adds an instruction, then waits
  the same way.
- `stop_session(session)`: stops a child.

How a child runs:

- `<python> -m kayden --headless --data-dir <data>/sessions/<id>`: its own data folder,
  with the parent's `config.toml` and `settings.toml` copied in, its spending limit set to
  `budget_usd` and its work folder set to the conversation's. Keys come from the same OS
  keychain. The permission ceiling passes down as for any child process.
- The manager starts its conversation and gives it the task as a goal, with short
  intervals (it checks in at least every 2 minutes), so a child that ends its turn
  without finishing continues soon.
- A permission prompt in the child is refused: nobody watches it yet. The refusal is shown
  in the session and in its report. A child does what its policy allows on its own.
- Cancelling the call (Stop, `op_cancel`) or `stop_session` stops the child; closing the
  parent stops every child.
- While it runs the call's progress line is the child's current step, so the step card in
  the parent conversation shows it live.

### What the user sees

- The manager keeps, per child, its task, state (working, waiting for an answer, done,
  failed, stopped), current step, spending and conversation as it arrives.
- **Desktop app:** a **Sub-sessions** list under the conversation list, for the open
  conversation, shown once there is one; clicking one opens a window with its
  conversation as text, updated live, and a Stop button. The status line counts the
  children working.
- **TUI:** `/sessions` lists them with the highlighted one's conversation and a Stop
  button; the status line counts the children working.

Choices are in `.agents/decisions.md` (Sub-sessions).
- The parent conversation shows each child as a step, its report as the result.

## Alternatives considered

- **A Tools module owns the children, outside the frontend.** No frontend change, but the
  frontend cannot see a module, so listing and peeking would need a new path from
  frontends through the runtime to that module.
- **The runtime owns them,** as it owns operations. Listing would be natural through
  RuntimeAPI, but the runtime would carry sub-sessions, which SPEC 1 keeps out of Kayden
  and every module and frontend would pay for.
- **Waiting for the KFM.** The right home for fleets, but not built, and a person using
  one window wants the Claude app's behaviour now.
- **Children in the same process.** Faster to start, but SPEC 4.3 has one agent per
  process, and a child's crash or runaway loop would take the parent with it.
- **Forwarding a child's permission prompts to the user.** Better, but every frontend
  would need a prompt UI for another agent's calls; refusing keeps the first version
  small.

## Consequences

- The agent can run side tasks in parallel without filling its own context, and the user
  watches and stops them as in the Claude app.
- **Cost.** Each child is a full agent with its own context. Its spending limit caps it,
  and the parent's own limit does not include it.
- **Unattended children do less.** A step that would prompt is refused; a child does only
  what its policy allows. Forwarding prompts is the KFM's escalation, not built.
- **Lost on quit.** Children stop with the window, and the list shows only children of
  this run; their data folders stay on disk.
- **Plain view.** A child's conversation is shown as text lines, not with the main view's
  bubbles and step groups.
- **Bundles.** A child is started as `python -m kayden` with the parent's interpreter; a
  packaged app whose interpreter cannot do that cannot start children.
