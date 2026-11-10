# Agent Note: Autonomy into sessions

Status: implemented — Continuous Mode is retired; autonomy is a per-conversation switch, and the user controls what the agent sees

## Problem

Kayden had two operating modes. Session Mode was an ordinary chat with many
conversations. Continuous Mode was one conversation per instance that never ended: the
agent could `idle()`, keep background work running, and wake itself when that work
finished (Agent-Controlled activation).

The real value of Continuous Mode is that autonomy, and autonomy does not need a separate
mode. What the mode added besides autonomy worked against the user:

- **The user could not curate what the agent sees.** A model is only as good as its
  context, and choosing that context is the user's main lever. A continuous agent had one
  conversation forever. A derailed thread, a wrong assumption or a finished project stayed
  in its summary and long-term memory, and the archive rule ("never delete") meant it was
  never really gone. There was no fresh start short of a new data folder.
- **Long-term memory was invisible and fixed.** Memory extracts "durable facts" during
  compaction. The user could look at them but not correct, add or remove one. A wrong fact
  was carried into every later conversation of that mode.
- **Two modes doubled the concepts.** A mode picker at launch, a mode fixed per process,
  separate stores, budgets, locks and logs per mode, `peek_other_mode` to bridge them, and
  "run two windows to use both". Non-technical users, the audience of the desktop app,
  should not have to understand any of it.

Separately, conversations could not be managed. Titles were always empty, and renaming and
deletion were out of scope. A long conversation list quickly became unusable.

The guiding principle: **the user controls what the agent sees.** Fresh starts at any time,
continuity when asked for, long-term memory the user can see and change, and autonomy as an
option on a conversation rather than a mode of the whole instance.

## Decision

### One kind of conversation

Every conversation is what a session was: the user can have many, open any of them, and
start a new one when no operation is outstanding. One process runs one agent with one open
conversation.

`Mode` and `Activation` are gone from the contracts (`api.py`, Draft 3). Every signature
that took a mode lost it: `Memory.create_conversation`, `list_conversations`, `search`,
`StartOptions`, `AgentConfig`, `ConversationInfo`. What was per (data folder, mode) is per
data folder: `instance.lock`, `usage.json` and `events.jsonl`.

### Autonomy is a conversation option

`ConversationInfo.autonomous`, default off, stored by Memory.

| | Off (default) | On |
|---|---|---|
| Who starts a cycle | Only the user | The user, and the end of awaited background work |
| `idle()` offered | No | Yes, with `AUTONOMY_PROMPT` added to the system prompt |

- `StartOptions.autonomous` sets it for a new conversation; None keeps a resumed one's.
- `RuntimeAPI.set_autonomous(bool)` changes the open conversation in any state, from the
  next turn. Turning it on while IDLE with operations running sets the wake set to them, so
  the agent wakes when they finish, as if it had idled waiting for them. Turning it off
  drops any pending wake-up; background operations keep running and their results wait for
  the user.
- `start()` always leaves the agent IDLE. A resumed autonomous conversation has no pending
  wake-up, so it never runs before the user sends a message or activates it.
- `activate()` is allowed in IDLE for every conversation.
- Budgets apply to autonomous cycles exactly as to user-started ones.

### Fresh starts and continuity

- **New conversation** is a fresh start: no messages, no summary, long-term memory
  included.
- **Continue from** (`StartOptions.seed_from`) starts a new conversation that knows where an
  earlier one left off. `ConversationInfo.seed_from` records the source. On the new
  conversation's first `maintain`, `SqliteMemory` runs the compaction prompt on the
  source's current summary and as much of its verbatim tail as the light model takes, and
  stores the result as the new conversation's first summary. This runs inside the first
  authorized cycle and is metered. The source is untouched.
- **Remember** (`ConversationInfo.remember`, default on, fixed at creation). With it off,
  long-term memory is neither assembled into the conversation nor extracted from it, and
  `recall(scope="all")` is refused. The archive still keeps every message.

### Long-term memory the user can see and change

One list per store, shared by every conversation with Remember on.

| RuntimeAPI | Memory | Effect |
|---|---|---|
| `memories()` | `memories()` | Current entries: id, text, source conversation, time |
| `add_memory(text)` | `remember(text)` | The user adds a fact |
| `edit_memory(id, text)` | `revise(id, text)` | The user corrects one; the earlier text is kept as a revision |
| `forget_memory(id)` | `forget(id)` | The entry and all its revisions are erased |

- Allowed in any state, applied from the next assemble. Each emits `MemoriesChanged` and is
  logged by entry id, without text.
- The Memory methods are optional: the base class raises `NotImplementedError`, which the
  runtime re-raises as "this memory module cannot edit memories", and frontends hide the
  controls.
- `SqliteMemory` keeps `memory_entries` and `memory_revisions`; the current text is the
  newest revision. A forgotten entry leaves only SHA-256 fingerprints of its texts
  (casefolded, whitespace collapsed), and compaction drops a new entry that matches one. A
  reworded fact is new to it, so the agent can learn a fact again from new messages.

### Searching other conversations

`peek_other_mode` is removed. `recall` takes `scope: "conversation" | "all"`. `"all"`
searches the open conversation and every conversation that is not archived and has
Remember on, and is refused (ERROR) with Remember off. `Memory.search` takes
`conversation_ids` (None = all), so the runtime decides the scope and Memory stays free of
that policy.

### Managing conversations

- **Automatic titles.** `maintain` writes a title for an untitled conversation from its
  first user message: a light-model call capped at 20 output tokens. A CoreError or an
  empty answer leaves the title empty for a later `maintain`; the write only fills an empty
  title, so a user's title is final. Until then `ConversationInfo.preview`, the start of
  the first user message, is what frontends show.
- **Rename** (`rename_conversation`; an empty title clears it), **archive**
  (`archive_conversation`; hidden from `list_conversations()` unless `include_archived`, and
  from `recall(scope="all")`; the open conversation stays open) and **delete**
  (`delete_conversation`, below). Each emits `ConversationUpdated(info)` or
  `ConversationDeleted(id)` and is logged without titles. Metadata changes go through one
  Memory method, `update_conversation`, and are not part of the append-only transcript
  archive.

### Deletion

The archive is append-only except for deletion the user asks for. Archive and forget
control what the agent sees; deletion controls what the disk keeps.

- `delete_conversation(id)` erases the conversation's messages, summaries, search index
  entries and metadata, and the long-term entries extracted from it with their revisions.
  It is refused for the open conversation, which is also the only one that can have
  outstanding operations. It is backed by the optional `Memory.delete_conversation`; without
  it the runtime says "this memory module cannot delete conversations".
- A conversation seeded from a deleted one keeps its own summary; its `seed_from` becomes
  None.
- `SqliteMemory` runs with `PRAGMA secure_delete = ON`. Deletion happens in one
  transaction: the conversation is listed in `erasing`, which the no-delete trigger on
  `messages` exempts; each message is removed from the contentless FTS index with its
  original text; everything else is deleted; the FTS index is optimized so no segment keeps
  the terms. Deletion and forgetting then checkpoint the write-ahead log (`TRUNCATE`).
- Docs say what deletion does not reach: backups and other copies outside the data folder,
  what the AI provider already received, and the activity log (`events.jsonl`), which
  records message text (contract issue A7).
- Frontends ask for confirmation naming the conversation, say it cannot be undone, and
  offer archive.

### Migrating existing data

`SqliteMemory` schema 3, migrated in place from schema 2 on open, in one transaction:

- `conversations` gains `autonomous`, `remember`, `archived` and `seed_from`. The continuous
  conversation becomes an ordinary one with `autonomous = 1` and the title "Continuous
  conversation". Its messages and summaries stay as they are. The `mode` column is dropped.
- `long_term` entries of both modes become one list in the new tables; exact duplicates
  collapse into the older entry.

The runtime locks `instance.lock`, and any `session.lock` or `continuous.lock` it finds,
before its first Memory call, so the store is migrated only once nothing else uses it. At
the first start on a data folder it also sums `usage-session.json` and
`usage-continuous.json` into `usage.json` when it does not exist yet, leaving the old files
in place, and starts `events.jsonl` beside the old logs. It refuses to start while an
older Kayden holds a lock, and an older Kayden cannot start while it runs.

`--mode` and `--activation` are accepted for one release with a warning naming
`--autonomous`; `--mode continuous` and `--activation agent_controlled` mean
`--autonomous`.

### Frontends

- **TUI.** The start screen has an Autonomous switch, Continue from and Remember instead of
  the mode and activation pickers. A conversation list offers open, rename, archive and
  delete with confirmation; a memory screen lists, adds, edits and forgets entries; the
  status bar shows when the open conversation is autonomous.
- **Desktop app.** The sidebar shows titles and offers rename, continue from, archive and
  delete; a private chat can be started; the open conversation has an autonomy switch with
  the spending next to it; a Memory window lists, adds, edits and forgets entries. See
  [Desktop app](../proposed/2026-11-07-desktop-app.md).

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
  turn already does. One switch is easier to explain.
- **Several conversations running at once in one process, or one lock per conversation so
  two processes share a data folder.** This would let an autonomous conversation work
  while the user chats in another, which the two-process setup allowed. A multi-agent
  runtime contradicts "one agent per process" and changes every `RuntimeAPI` command.
  Per-conversation locks need the budget file to be shared safely between processes. Both
  are larger than this change; running a second instance with its own data folder still
  works, without shared memory.
- **Titles from the first message only, with no inference.** Free and deterministic, and
  it is the fallback (`preview`). Model-written titles read much better in a long list, for
  one small light-model call per conversation.
- **Editing long-term memory rows in place.** Simpler, but an edit would silently replace
  what the agent had learned. Revisions keep each change visible and allow an undo later.
- **Archive and forget only, no deletion.** Keeps the archive strictly append-only, so
  nothing is ever lost by accident. But anything sensitive pasted into a chat would stay on
  disk forever, and the user would control what the agent sees but not what Kayden keeps.
  Deletion is explicit and confirmed, so accidental loss stays unlikely.
- **Deleting single messages.** Finer control, but a hole in a transcript breaks the
  pairing of tool calls and results, and summaries already include the message. Deleting
  the conversation is the unit that leaves nothing inconsistent.
- **Keeping the `mode` column after migration.** Unused, and new rows would need a made-up
  value; SQLite drops a column in place.

## Consequences

- **One coordinated break.** Every module, the TUI, the desktop app, the launcher and most
  tests changed together, under contract Draft 3. Nothing outside this repository
  implemented the contracts, so nothing else broke.
- **The user curates the context.** New, Continue from, Remember, archive, forget and
  delete each change what the agent sees; long-term memory is visible and correctable.
- **Deletion is real but bounded.** Content is overwritten in `memory.db` and the
  write-ahead log, but not in backups, at the provider, or in the activity log, which
  records message text. That last gap is open (contract issue A7); docs tell the user how to
  remove the log.
- **Forgetting is by wording.** A forgotten fact does not come back in the same words; a
  reworded one can, and the user forgets it again. Forgetting a fact does not delete the
  messages it came from.
- **Autonomy without supervision spends money.** Budgets still apply and the switch is off
  by default; the desktop app shows spending next to it.
- **The side-by-side setup is gone.** A user who ran a continuous agent next to a session
  in one data folder now runs two data folders, without shared memory.
- **Titles cost a call.** Each new conversation spends one small light-model call, metered
  like any maintenance.
