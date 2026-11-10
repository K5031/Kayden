# Agent Note: Autonomy into sessions

Status: implemented — Continuous Mode is retired; every conversation wakes when its background work finishes, and the user controls what the agent sees

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
continuity when asked for, long-term memory the user can see and change, and the one useful
part of autonomy, carrying on when background work finishes, in every conversation rather
than in a mode of the whole instance.

## Decision

### One kind of conversation

Every conversation is what a session was: the user can have many, open any of them, and
start a new one when no operation is outstanding. One process runs one agent with one open
conversation.

`Mode` and `Activation` are gone from the contracts (`api.py`, Draft 3). Every signature
that took a mode lost it: `Memory.create_conversation`, `list_conversations`, `search`,
`StartOptions`, `AgentConfig`, `ConversationInfo`. What was per (data folder, mode) is per
data folder: `instance.lock`, `usage.json` and `events.jsonl`.

### Every conversation wakes when its background work finishes

There is no autonomy setting. When a cycle ends, the wake set is every outstanding
operation; once all of them have terminated the runtime starts the next cycle by itself,
so the agent reports on work it started without the user asking whether it is done. This
is the only time the agent acts without a user message.

- The system prompt and the tools are the same for a conversation's whole life.
- `start()` always leaves the agent IDLE. A resumed conversation has no pending wake-up,
  so it never runs before the user sends a message or activates it.
- Stop clears a pending wake-up.
- `activate()` is allowed in IDLE for every conversation.
- Permissions and budgets apply to a woken cycle exactly as to a user-started one.

### Fresh starts and continuity

- **New conversation** is a fresh start: no messages, no summary, long-term memory
  included.
- **Continue from** (`StartOptions.seed_from`) starts a new conversation that knows where an
  earlier one left off. `ConversationInfo.seed_from` records the source. On the new
  conversation's first `maintain`, `SqliteMemory` runs the compaction prompt on the
  source's current summary and as much of its verbatim tail as the light model takes, and
  stores the result as the new conversation's first summary. This runs inside the first
  authorized cycle and is metered. The source is untouched.

### Long-term memory the user can see and change

One list per store, shared by every conversation.

| RuntimeAPI | Memory | Effect |
|---|---|---|
| `memories()` | `memories()` | Current entries: id, text, source conversation, time |
| `add_memory(text)` | `remember(text)` | The user adds a fact |
| `edit_memory(id, text)` | `revise(id, text)` | The user corrects one; the earlier text is kept as a revision |
| `forget_memory(id)` | `forget(id)` | The entry and all its revisions are erased |

- Allowed in any state, applied from the next assemble. Each emits `MemoriesChanged` and is
  logged by entry id, without text.
- The agent edits the same list with two built-in tools: `remember(text)` adds an entry
  from the open conversation (`Memory.remember(text, conversation_id=)`), so deleting
  that conversation erases it; `forget(text)` erases the entries whose text matches, case
  and whitespace ignored, since the prompt shows entries without ids. Both are never gated,
  appear in the conversation as tool calls, and are offered only when the Memory module
  can edit memories. Their descriptions carry the guidance (lasting facts about the user,
  never secrets), so it holds with any instructions.
- The Memory methods are optional: the base class raises `NotImplementedError`, which the
  runtime re-raises as "this memory module cannot edit memories", and frontends hide the
  controls.
- `SqliteMemory` keeps `memory_entries` and `memory_revisions`; the current text is the
  newest revision. A forgotten entry leaves only SHA-256 fingerprints of its texts
  (casefolded, whitespace collapsed), and compaction drops a new entry that matches one. A
  reworded fact is new to it, so the agent can learn a fact again from new messages.

### Searching other conversations

`peek_other_mode` is removed. `recall` takes `scope: "conversation" | "all"`. `"all"`
searches the open conversation and every conversation that is not archived. `Memory.search` takes
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
  outstanding operations. Frontends let the user delete the open conversation anyway: they
  start a new chat first (refused while the agent works, like any switch), then delete the
  old one. It is backed by the optional `Memory.delete_conversation`; without
  it the runtime says "this memory module cannot delete conversations".
- A conversation seeded from a deleted one keeps its own summary; its `seed_from` becomes
  None.
- `SqliteMemory` runs with `PRAGMA secure_delete = ON`. Deletion happens in one
  transaction: the conversation is listed in `erasing`, which the no-delete trigger on
  `messages` exempts; each message is removed from the contentless FTS index with its
  original text; everything else is deleted; the FTS index is optimized so no segment keeps
  the terms. Deletion and forgetting then checkpoint the write-ahead log (`TRUNCATE`).
- The event log holds no content, so deletion leaves nothing there: messages, tool calls
  and results are logged by id, kind and size, and titles, previews and the system prompt
  are left out.
- Docs say what deletion does not reach: backups and other copies outside the data folder,
  and what the AI provider already received.
- Frontends ask for confirmation naming the conversation and say it is permanent.

### Stored data

`SqliteMemory` refuses a store of another version. Lock, usage and
event log are one each per data folder. Kayden carries no code for data folders or
command-line flags of the per-mode versions.

### Frontends

- **TUI.** The start screen has Continue from instead of the mode and activation pickers.
  A conversation list offers open, rename, archive and delete with confirmation; a memory
  screen lists, adds, edits and forgets entries.
- **Desktop app.** The sidebar shows titles and offers rename, continue from, archive and
  delete; the spending is shown above the conversation; a Memory window lists, adds, edits and forgets entries. See
  [Desktop app](../proposed/2026-11-07-desktop-app.md).

## Alternatives considered

- **An Autonomous mode per conversation** (a switch that woke the agent after background
  work and told it to work without asking). Built and removed. Once waking up applied to
  every conversation, the switch changed one paragraph of instructions; the user can say
  the same in the chat or in the instructions in Settings. Two variants were dropped on
  the way: switching at any time (each switch changed the system prompt and the tools
  mid-conversation, so Claude rejected its earlier thinking blocks and the prompt cache
  was lost) and choosing only before the first message (a lock to explain for no
  behavior).
- **Holding background results until the user's next message.** The agent then never
  acts unprompted, but a user who asked for something slow has to ask "is it done?", which
  reads as the agent forgetting. A wake-up only follows work started in that conversation
  and is bounded by the spending limit.
- **An `idle()` tool for the agent to end its cycle**, optionally naming the operations to
  wait for. Claude Opus 5.5 returns text that precedes a tool call as a hidden progress
  note, so an agent that replied and then called `idle()` appeared to say nothing; told
  not to, it still did. Ending a turn without tool calls already ends the cycle. The cost
  is that the agent waits for all of its background work, never a part of it.
- **A goal loop** (the runtime checks after each turn whether a stated goal is met and
  sends the agent back to work). The one thing that would make an autonomous mode differ
  in behavior without touching permissions; not built, to be judged on real use.
- **Loosening permissions for hands-off runs** (read freely, or anything inside the work
  folder). The shell tool is not a sandbox, and the permission level stays one setting, in
  Settings.
- **Timed wake-ups with no message from the user** (an agent that looks around and acts
  under standing instructions). A different product, closer to a scheduled assistant;
  open-ended spending and no task to finish.
- **A Remember switch per conversation** (off = a private chat that neither uses nor adds
  to long-term memory and cannot search other conversations). The owner did not ask for
  it; forgetting an entry and deleting a conversation already cover what the agent keeps.
- **Keep Continuous Mode and add conversation management to sessions.** The smallest
  change. It leaves the unresettable conversation, the invisible long-term memory and the
  two-mode concepts in place, which are the problem.
- **Keep Continuous Mode with a "start over" command.** A reset would be a new
  conversation under another name, plus a mode picker, separate stores and
  `peek_other_mode`. One kind of conversation gives the same thing with fewer concepts.
- **Several conversations running at once in one process, or one lock per conversation so
  two processes share a data folder.** This would let one conversation work
  while the user chats in another, which the two-process setup allowed. A multi-agent
  runtime contradicts "one agent per process" and changes every `RuntimeAPI` command.
  Per-conversation locks need the budget file to be shared safely between processes. Both
  are larger than this change; running a second instance with its own data folder still
  works, without shared memory.
- **Titles from the first message only, with no inference.** Free and deterministic, and
  it is the fallback (`preview`). Model-written titles read much better in a long list, for
  one small light-model call per conversation.
- **Learning facts only during compaction.** No extra cost, but most conversations never
  grow long enough to be compacted, so the agent learned almost nothing across
  conversations. A separate extraction request after each conversation was the other way;
  the `remember` tool costs nothing extra and the user sees each fact as it is kept.
- **Editing long-term memory rows in place.** Simpler, but an edit would silently replace
  what the agent had learned. Revisions keep each change visible and allow an undo later.
- **Archive and forget only, no deletion.** Keeps the archive strictly append-only, so
  nothing is ever lost by accident. But anything sensitive pasted into a chat would stay on
  disk forever, and the user would control what the agent sees but not what Kayden keeps.
  Deletion is explicit and confirmed, so accidental loss stays unlikely.
- **Deleting single messages.** Finer control, but a hole in a transcript breaks the
  pairing of tool calls and results, and summaries already include the message. Deleting
  the conversation is the unit that leaves nothing inconsistent.
- **Keeping migration code for the per-mode versions** (schema 2 stores, per-mode usage,
  lock and log files, `--mode` and `--activation`). The only data folder those versions
  wrote has been converted, so the code would never run again.

## Consequences

- **One coordinated break.** Every module, the TUI, the desktop app, the launcher and most
  tests changed together, under contract Draft 3. Nothing outside this repository
  implemented the contracts, so nothing else broke.
- **The user curates the context.** New, Continue from, archive, forget and delete
  each change what the agent sees; long-term memory is visible and correctable.
- **Deletion is real but bounded.** Content is overwritten in `memory.db` and the
  write-ahead log, and the event log never holds it, but backups and the provider are out
  of reach. The content-free log is less useful for debugging what the agent said or ran;
  the conversation itself has that until it is deleted.
- **Forgetting is by wording.** A forgotten fact does not come back in the same words; a
  reworded one can, and the user forgets it again. Forgetting a fact does not delete the
  messages it came from.
- **The agent can spend without a new message.** A wake-up costs a cycle the user did not
  start by hand. It follows only work the user asked for, budgets apply, and the desktop
  app shows spending above the conversation.
- **The side-by-side setup is gone.** A user who ran a continuous agent next to a session
  in one data folder now runs two data folders, without shared memory.
- **Titles cost a call.** Each new conversation spends one small light-model call, metered
  like any maintenance.
