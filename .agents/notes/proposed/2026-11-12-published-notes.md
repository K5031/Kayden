# Agent Note: Published notes

Status: proposed — a Memory module in which an agent keeps its own notes and also sees notes another agent published for it, which it can read but not change; not to be built before a run shows it is needed

Sits on [Memory like Claude Code](../implemented/2026-11-11-memory-like-claude-code.md).

## Problem

A Kadence's long-term memory is its own: a folder of notes only it reads and writes. There
is no way for one Kadence to know what another has learned, except by being told in a
message.

The case that asks for it is a fleet (KFM, the "Shared team memory" note there): a
supervising agent reads a project and works something out, and every agent it starts
finds the same thing out again from nothing. The same holds outside a fleet for anyone
who runs several Kadences on one body of work.

Letting every agent read and write one folder is the obvious answer and the wrong one:

- **A supervisor's notes are not for its agents.** They hold what it thinks of their
  work, what its user said in confidence, the budget, plans not yet decided. Most of the
  rest is noise to an agent with one task, paid for at the start of every conversation.
- **A note written below is read above as fact.** An agent that was misled, by a page it
  read or by its own mistake, writes into what every other agent starts with. Nobody
  chose to pass it on.

## Proposal

Notes flow one way: an agent publishes, others read.

- **A module of its own, `kadence.memory.published`**, built on `kadence.memory.files` the
  way that one is built on `SqliteArchive`. The agent's own memory is that module's,
  unchanged: its folder, its rules, its `memory` tool.
- **Two options**, both folders of notes in the same format (an index and one file per
  note):
  - `reads`: folders whose notes this agent sees and cannot change. Their indexes are
    shown beside its own, each under a heading that says whose notes they are and that
    they are read-only; the `memory` tool views their files and refuses every other
    command on them.
  - `publishes`: one folder this agent writes for others, with the commands and guards
    its own memory has. The rules say what belongs there: what the readers need to know
    to do their work, never what is about them or not for them.
- **Nothing about fleets in Kadence.** Who publishes to whom is configuration. KFM's
  Kadence harness writes it per unit: a unit reads what the units above it publish, and a
  unit that can have agents publishes for them.
- What an agent learns still goes up in its report; whoever reads the report decides
  whether it becomes a published note.

## Alternatives considered

- **One memory folder for all** (`kadence.memory.files` with every instance's `projects`
  pointing at one place). A settings change and nothing to build; it fails on both counts
  above.
- **Two options on `kadence.memory.files` itself.** Less code than a module. But that
  module is Claude Code's memory, kept the same on purpose, and every user of it would
  carry rules and a tool path for something only some setups have.
- **Notes as fleet tools in KFM** (publish and read through its tool server). Works for
  any agent program, not only Kadence, and KFM decides who may write. But the notes would
  not be in view when a conversation starts: an agent would have to think to look, and
  KFM would rebuild what Memory already has (the index, its limits, read before write).
- **Everything in messages**, as now. Nothing is known that the sender did not think to
  pass on, and it is written and read again for every agent.

## Acceptance criteria

- A note one instance publishes is in what another instance, configured to read that
  folder, starts its next conversation with.
- The reading instance cannot create, change, rename or delete a published note, and its
  own notes are not visible to the publisher.
- An instance without the two options behaves as `kadence.memory.files` does.
- A fleet's job costs less with published notes than without, measured.

## Risks

- **Not shown to be needed.** No run has had agents pay to learn the same thing twice;
  the agents below a lead have lived for two minutes.
- **Reading is paid for.** Every reader carries the published index in every request;
  it needs the same limits as an agent's own.
- **An open conversation does not see new notes.** The index is read when a conversation
  is first assembled; an agent that runs for hours sees what was published since only if
  it looks.
- **A wrong published note misleads every reader**, and they cannot correct it; only a
  report upward can.
- A reader several ranks down reads several folders: more to carry, and nothing settles
  which note wins when two disagree.
