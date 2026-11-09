# Agent Note: Autonomy into sessions

Status: proposed — retire Continuous Mode; autonomy becomes a per-conversation option, and the user controls what the agent sees

## Problem

Kayden has two operating modes (SPEC 4). Session Mode is an ordinary chat with many
conversations. Continuous Mode is one conversation per instance that never ends: the agent
can `idle()`, keep background work running, and wake itself when that work finishes
(Agent-Controlled activation).

The real value of Continuous Mode is that autonomy, and autonomy does not need a separate
mode. What the mode adds besides autonomy works against the user:

- **The user cannot curate what the agent sees.** A model is only as good as its context,
  and choosing that context is the user's main lever. A continuous agent has one
  conversation forever. A derailed thread, a wrong assumption or a finished project stays
  in its summary and long-term memory, and the archive rule ("never delete") means it is
  never really gone. There is no fresh start short of a new data folder.
- **Long-term memory is invisible and fixed.** Memory extracts "durable facts" during
  compaction (`SqliteMemory.maintain`). The user can look at them (`inspect_memory`) but
  cannot correct, add or remove one. A wrong fact is carried into every later conversation
  of that mode.
- **Two modes double the concepts.** A mode picker at launch, a mode fixed per process,
  separate stores, budgets, locks and logs per mode, `peek_other_mode` to bridge them, and
  "run two windows to use both". Non-technical users, the audience of the desktop app,
  should not have to understand any of it.

Separately, conversations cannot be managed. Titles are always empty (the TUI shows
"Untitled <id>"), and renaming is out of scope (SPEC 13, 16.5). A long conversation list
quickly becomes unusable.

The guiding principle: **the user controls what the agent sees.** Fresh starts at any time,
continuity when asked for, long-term memory the user can see and change, and autonomy as an
option on a conversation rather than a mode of the whole instance.

## Proposal

### One kind of conversation

Continuous Mode is retired. Every conversation is what a session is today: the user can
have many, open any of them, and start a new one when no operation is outstanding. One
process still runs one agent with one open conversation.

`Mode` and `Activation` leave the contracts. Every signature that takes a mode loses it:
`list_conversations()`, `create_conversation()`, `search()`, `StartOptions`, `AgentConfig`,
`ConversationInfo`. This is a breaking change (contract Draft 3.0). SPEC 16.1 asks for
additive evolution, but every module that implements the contracts lives in this
repository, so the break costs one coordinated change and nothing outside.

What was per (data folder, mode) becomes per data folder: the instance lock
(`instance.lock`), usage (`usage.json`) and the event log (`events.jsonl`).

### Autonomy is a conversation option

`ConversationInfo.autonomous: bool`, default off, stored by Memory with the conversation.

| | Off (default) | On |
|---|---|---|
| Who starts a cycle | Only the user | The user, and the end of awaited background work |
| `idle()` offered | No | Yes, with `CONTINUOUS_PROMPT` (renamed `AUTONOMY_PROMPT`) |
| Behaves like | Session Mode today | Agent-Controlled Continuous Mode today |

User-Controlled Continuous Mode is dropped rather than carried over: with no wake-ups it
behaves like a session that also has `idle()`, and `idle()` without wake-ups only tells the
model to stop, which ending a turn already does.

- `StartOptions.autonomous: bool | None`: set it for a new conversation; None keeps a
  resumed conversation's value.
- `RuntimeAPI.set_autonomous(bool)` changes the open conversation in any state, from the
  next turn. Turning it off clears any pending wake-up, so nothing restarts on its own;
  background operations keep running and their results wait for the user, as in Session
  Mode today.
- After a restart an autonomous conversation opens idle and never wakes before the user
  sends a message or activates it (SPEC 11's rule for continuous agents, kept).
- Budgets are unchanged and apply to autonomous cycles exactly as to user-started ones.

### Fresh starts and continuity

- **New conversation** is a fresh start, as today: no messages, no summary, long-term
  memory included.
- **Continue from** (`StartOptions.seed_from: ConversationId`) starts a new conversation
  that knows where an earlier one left off. Memory records the source. On the new
  conversation's first `maintain`, it writes the initial rolling summary from the source's
  current summary and verbatim tail, with the same compaction prompt and the light model.
  This runs inside the first authorized cycle and is metered, like any compaction. The
  source is untouched, and the new conversation's own archive starts empty.
- **Remember** (`ConversationInfo.remember: bool`, default on, set at start). With it off,
  long-term memory is neither put into this conversation's context nor extracted from it,
  and `recall` stays inside the conversation. This is a temporary chat. The archive still
  keeps every message.

### Long-term memory the user can see and change

Long-term memory becomes one list for the whole instance, shared by every conversation with
Remember on.

New `RuntimeAPI` methods, backed by new `Memory` methods:

| RuntimeAPI | Memory | Effect |
|---|---|---|
| `memories()` | `memories()` | Current entries: id, text, source conversation, time |
| `add_memory(text)` | `remember(text)` | The user adds a fact |
| `edit_memory(id, text)` | `revise(id, text)` | The user corrects one |
| `forget_memory(id)` | `forget(id)` | The entry stops reaching any context |

- They are allowed in any state and apply from the next assemble. Each emits
  `MemoriesChanged` and is logged (without the text, which may be private).
- The Memory methods are optional: the base class raises `NotImplementedError`, and the
  runtime reports that as "this memory module cannot edit memories". Frontends hide the
  controls when `memories()` is not supported.
- `SqliteMemory` keeps `long_term` append-only, like the transcript archive: an edit or a
  forget is a new revision row, and the current list is the latest revision of each entry
  that is not forgotten. Compaction sees only the current list, so it does not re-extract a
  forgotten fact from the same summary it was removed from. It can still learn it again
  from new messages; the user then forgets it again or tells the agent not to remember it.
- `inspect_memory` keeps showing the rolling summary of a conversation, read-only.

### Searching other conversations

`peek_other_mode` is removed. `recall` gains `scope: "conversation" | "all"` (default
`"conversation"`). `"all"` searches every conversation that is not archived and has
Remember on, and is refused in a conversation with Remember off. This keeps the one
capability `peek_other_mode` gave, reading other work, under the user's control.

### Managing conversations

- **Automatic titles.** When a conversation has no title, Memory writes one during
  `maintain`, once the first user message is in: a light-model call capped at 20 output
  tokens. A failed or empty call leaves the title empty and is retried on the next
  `maintain`; frontends then show the start of the first message. The call is metered and
  counts against the budget like any maintenance. Memory never overwrites a title that is
  set, so a user's title is final.
- **Rename.** `RuntimeAPI.rename_conversation(id, title)`, any state, any conversation.
  An empty title clears it, so an automatic title follows.
- **Archive.** `RuntimeAPI.archive_conversation(id, archived: bool)`. An archived
  conversation is hidden from `list_conversations()` (unless `include_archived=True`) and
  from `recall(scope="all")`, and can be unarchived. The open conversation can be archived;
  it stays open until the user switches away.
- **No deletion.** Deleting conversations, messages or archived memories stays out of
  scope: it needs an exception to the append-only archive rule in `AGENTS.md`, which is the
  owner's call and gets its own note if wanted. Forgetting covers what the agent sees;
  deletion would cover what the disk keeps.

Each change emits `ConversationUpdated(info)` and is logged. These all go through one new
Memory method, `update_conversation(id, *, title=None, archived=None, autonomous=None)`.
Conversation metadata is not part of the append-only transcript archive.

### Migrating existing data

`SqliteMemory` schema version 2, migrated in place on open, in one transaction:

- `conversations` gains `autonomous`, `remember`, `archived` and `seed_from`. A continuous
  conversation becomes an ordinary one with `autonomous = 1`; its title is set to
  "Continuous conversation" so the user can find it. Its messages and summaries stay as
  they are.
- `long_term` entries of both modes become one list. Exact duplicates collapse into the
  older entry. The `mode` column stays and is ignored.

The runtime, at the first start on a data folder that has per-mode files:

- sums `usage-session.json` and `usage-continuous.json` into `usage.json`, so a spending
  limit counts what was already spent, and leaves the old files in place;
- starts a new `events.jsonl`; the old logs stay;
- refuses to start while an old per-mode lock is held by a running older Kayden.

The `--mode` and `--activation` flags are accepted for one release with a warning that
names the replacement (`--autonomous`), then removed.

### What changes elsewhere

- **SPEC.** Sections 1, 2, 4 (one kind of conversation; autonomy replaces modes and
  activation policies), 5 (`idle()` and batch wake-up apply to autonomous conversations),
  8.2 and 8.8 (no mode isolation; Remember and memory editing), 9 and 10.4 (per data
  folder), 11, 12 (Scenarios C and E use an autonomous conversation; Scenario I becomes
  "recall across conversations, never in a temporary chat"), 13 (renaming leaves the
  out-of-scope list; deletion stays) and 16.
- **TUI.** The start screen loses the mode and activation pickers and gains an
  "Autonomous" switch, "Continue from" and "Remember". The conversation list shows
  titles and offers rename and archive. A memory screen lists, adds, edits and forgets
  entries.
- **Desktop app.** v1 has no mode choice (see [Desktop app](2026-11-07-desktop-app.md)).
  The controls above appear in it as they land in the runtime.
- **Docs.** `concepts.md` replaces Modes and Activation with Conversations, Autonomy and
  Memory; `using-kayden.md` loses `--mode` and `--activation`.

## Alternatives considered

- **Keep Continuous Mode and add conversation management to sessions.** The smallest
  change. It leaves the unresettable conversation, the invisible long-term memory and the
  two-mode concepts in place, which are the problem.
- **Keep Continuous Mode with a "start over" command.** A reset would be a new
  conversation under another name, plus a mode picker, separate stores and
  `peek_other_mode`. Autonomy per conversation gives the same thing with fewer concepts.
- **Autonomy as an instance setting instead of per conversation.** Simpler storage, but
  then every chat in the instance may wake itself, which is the wrong default for a quick
  question asked next to a long-running task.
- **Keep both activation policies per conversation.** User-Controlled autonomy differs
  from a normal conversation only by `idle()`, which without wake-ups does what ending a
  turn already does (see the table above). One switch is easier to explain.
- **Several conversations running at once in one process, or one lock per conversation so
  two processes share a data folder.** This would let an autonomous conversation work
  while the user chats in another, which today's two-process setup allows. A multi-agent
  runtime contradicts "one agent per process" and changes every `RuntimeAPI` command.
  Per-conversation locks need the budget file to be shared safely between processes. Both
  are larger than this change; running a second instance with its own data folder still
  works, without shared memory.
- **Titles from the first message only, with no inference.** Free and deterministic, and
  it stays the fallback. Model-written titles read much better in a long list, for one
  small light-model call per conversation.
- **Editing long-term memory rows in place.** Simpler, but the store would then have one
  table that silently changes. Revisions keep the store append-only and allow an undo
  later.
- **Real deletion of conversations and memories.** Users expect it, and it matters for
  privacy. It contradicts the append-only archive rule in `AGENTS.md`, so it is left to
  the owner and a separate note rather than slipped in here.

## Acceptance criteria

- Tests, written first:
  - an autonomous conversation idles with a batch outstanding and wakes once when it
    finishes (Scenario E); a non-autonomous one is not offered `idle()` and never wakes
  - `set_autonomous(False)` while idle with a pending wake-up: the batch finishes and
    nothing starts
  - after a restart an autonomous conversation opens idle and does not wake before the
    user acts
  - a seeded conversation's first context contains the source's summary, and the source
    is unchanged
  - with Remember off, long-term memory is not assembled, not extracted, and
    `recall(scope="all")` is refused
  - add, edit and forget change the next assembled context; a forgotten entry is not
    re-extracted from the existing summary; the `long_term` table only grows
  - automatic titles are written once, never over a user's title, and a failed title call
    changes nothing
  - archived conversations are absent from `list_conversations()` and `recall(scope="all")`
  - migration from schema 1 with both modes keeps every message, turns the continuous
    conversation into an autonomous one, merges long-term memory and sums usage
- `SqliteMemory` passes the Memory conformance suite, extended for the new methods; the
  fake memory in `tests/fakes/` implements them too.
- SPEC, `api.py`, the TUI and `docs/` change in the same change as the runtime.

## Risks

- **The break touches every module and most tests.** Mode appears throughout the
  runtime, Memory, the TUI and the launcher. Mitigation: one change, done before the
  desktop app grows beyond Session Mode, with the scenario tests rewritten first.
- **Remember is easy to misread.** A user may think it hides the conversation from the
  archive. The UI says "This chat won't use or add to long-term memory", and docs say the
  messages are still kept.
- **Forgetting is not erasing.** A forgotten fact stays on disk in the revision history and
  in the archived messages it came from. Until deletion is decided, docs must say so
  plainly.
- **Autonomy without supervision spends money.** A wake-up loop can run up a bill. Budgets
  still apply, and the switch is off by default; the desktop app should show spending
  next to it.
- **Losing the side-by-side setup.** A user who runs a continuous agent next to a session
  today loses the shared memory between them. This is the cost named under Alternatives.
