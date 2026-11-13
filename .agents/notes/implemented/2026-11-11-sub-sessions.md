# Agent Note: Sub-sessions

Status: implemented — the frontend hosts sub-sessions: it starts headless Kayden children for the agent, lists them under their conversation, and opens each like a conversation of its own

## Problem

The user uses an app (the Claude desktop app) where an agent hands self-contained tasks to
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
  context, in the conversation's work folder, on the task as a goal. The agent calls it
  when the user asked for the work; when it is its own idea it proposes the call
  ([proposals](2026-11-11-proposals.md)), as the Claude app asks before a session the agent
  proposes. The call runs in the background and returns the child's report when the goal
  is reached, or its question when it asks for help.
- `message_session(session, text)`: answers a question or adds an instruction, then waits
  the same way.
- `stop_session(session)`: stops a child.

How a child runs:

- `<python> -m kayden --headless --data-dir <data>/sessions/<id>`: its own data folder,
  with the parent's `config.toml` and `settings.toml` copied in, its spending limit set to
  `budget_usd` when it has one (none by default, never the parent's) and its work folder
  set to the conversation's. Keys come from the same OS
  keychain. The permission ceiling passes down as for any child process.
- The manager starts its conversation and gives it the task as a goal, with short
  intervals (it checks in at least every 2 minutes), so a child that ends its turn
  without finishing continues soon.
- A permission prompt in the child waits for the user, as in the parent; the parent's call
  keeps waiting meanwhile.
- Cancelling the call (Stop, `op_cancel`) or `stop_session` stops the child; closing the
  parent stops every child that is working. A child that is done or stopped is closed
  once no view has it open.
- Each child's record (`session.json` in its folder) keeps it listed after a restart.
  Opening one that is not running starts it again on its own conversation; **Resume** also
  lets it carry on with its goal. Nothing resumes by itself after a restart (SPEC 11).
- When a child stops working and no call of the parent's waits for it (the user talked to
  it), the parent gets a notice in its conversation, as the Claude app sends a note when a
  session ends a turn.
- While it runs the call's progress line is the child's current step, so the step card in
  the parent conversation shows it live.

### Opening a child

`SubSessions.open(child)` returns a `RemoteRuntime` (`kayden.frontends.remote`): a
RuntimeAPI over the child's channel. Every command but `restart` is a request;
`snapshot()`, which must answer at once, is read on opening and kept current from the
events. `kayden.frontends.wire` turns the channel's JSON back into API values. A frontend
therefore shows a child with the same code as its own conversation.

### What the user sees

- **Desktop app:** the sidebar is a tree, as in the Claude app. Conversations are grouped
  by project (their folder) under headings that fold, and each conversation's children
  are listed under it, with a dot while they work and their state as the second line.
  Clicking a child shows it in the conversation area: its messages and steps, its
  permission cards, Stop, and a message box that talks to it. Clicking a conversation goes
  back; the child keeps working. Memory, tool servers and settings stay the window's own.
- **Row actions:** open, resume, stop, rename, archive and delete.
- **TUI:** `/sessions` lists the children with the same actions; opening one shows it in
  place of the conversation, the app's runtime swapped for the child's, until `/back`.
- Both status lines count the children working; the parent conversation shows each child
  as a step, with its report as the result.

Choices are in `.agents/decisions.md` (Sub-sessions).

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
- **Refusing a child's permission prompts,** since nobody watches it. Simpler, and shipped
  first, but a child could then do only what its policy allowed, and the user wanted the
  Claude app's behaviour, where a session asks the user.
- **A window per child,** or a plain text view of its work. Less code than switching the
  conversation area, but not what the Claude app does, and the plain view could not show
  steps, prompts or a message box.
- **Resuming children by themselves after a restart,** as long-running app sessions do.
  Convenient, but SPEC 11 keeps a crash from quietly repeating an action; one click is the
  price.
- **A notice for every turn,** including a goal's check-ins. Closest to the Claude app's
  notes, but a child between check-ins would wake its parent every few minutes for nothing.
- **A real tree widget** for the sidebar. Native folding, but the rows' drawing, menus and
  shortcuts are built on a list; headings and indented rows in the same list keep them.

## Consequences

- The agent can run side tasks in parallel without filling its own context, and the user
  opens, talks to, approves and stops each as in the Claude app.
- **Cost.** Each child is a full agent with its own context. A spending limit caps it only
  when the user or the agent sets one, as with Claude's sub-agents; the parent's own limit
  does not include it.
- **Children wait for approvals.** A child whose step needs the user's OK waits until the
  user opens it; the sidebar says so. Routing prompts to a supervisor agent is the KFM's
  escalation, not built.
- **Stopped on quit.** Working children stop with the window; they stay listed, and a click
  on Resume, not Kayden itself, lets them carry on.
- **Notices wake the parent.** Each notice is a request the parent pays for.
- **The channel protocol carries all of RuntimeAPI,** so it is a larger contract: a
  renamed API type or field breaks reopening a child run by an older Kayden.
- **Bundles.** A child is started as `python -m kayden` with the parent's interpreter; a
  packaged app whose interpreter cannot do that cannot start children.
