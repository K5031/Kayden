# Agent Note: Memory as notes

Status: implemented — long-term memory is a profile and topic notes, an index always in view, saved after each answer

## Problem

Long-term memory was one flat list of short facts ("Is vegetarian"), all of it in every
prompt. It had three problems:

- **It did not scale.** Every fact cost tokens in every request and added noise, however
  unrelated to the conversation.
- **It learned rarely.** New facts were extracted only when a long conversation was
  compacted, and most conversations never got that long; otherwise only what the model
  happened to `remember` was kept.
- **A fact was one line.** "The Kadence project: what it is, what was decided, what is
  next" did not fit.

Agents that remember well keep topic files with a small index always in view and open a
file when it is relevant (Claude Code's memory: a `MEMORY.md` index and one file per fact;
Anthropic's memory tool: a folder of files the model checks before a task), and save as
they go (Claude's app saves "individual topics as you chat"). This copies that pattern
(the user, 2026-11-10).

## Decision

### What is kept

- **Notes** (`api.MemoryNote`): a title, a one-line description and a text, one per
  project, person or topic. Each change is a revision; a note remembers the conversation
  that last wrote it, and keeps it as none when that conversation is deleted.
- **The profile**: the note titled `api.PROFILE` ("About you"), for who the user is and how
  they want answers. It always exists; it can be emptied but not deleted or renamed.

### What every request sees

The system prompt gets the profile in full, then the index, one line per other note
("title: description", most recently written first), within the long-term budget. Note
texts are not loaded.

### Tools

Memory's tools (`kadence.memory.tools`): `recall` (the archive, as before), `read_around`
(a recall hit with the messages before and after it), `read_note`,
`save_note` (creates, or replaces the note with that title, case ignored; the model reads
a note before changing it) and `forget_note`.

### Saving after each answer

The runtime calls `Memory.maintain` once more after a cycle's last answer, before going
idle, and emits `MemoriesChanged` when it acted. The SQLite Memory then runs its save
pass when the newest message is an answer that calls no tool and comes after the last
saved one: the light model reads the messages since, with the profile, the notes' texts
that fit a budget and the index of the rest, and replies with `<save>` changes (a whole
text) and `<append>` ones (for notes it was shown by title only), or nothing. The prompt
asks for what the user said or decided, not the agent's suggestions, and nothing the
exchange does not state. A failed request is logged and the exchange read again after the
next answer. Compaction writes only the rolling summary.

### The user's side

`RuntimeAPI.notes`, `save_note` and `delete_note`. The Memory window in both apps lists
notes by title and description and opens one in an editor.

### No migration

The SQLite store moved to schema 7; a store of schema 6 is refused, as for every schema
change. The user's store is converted once, by hand.

Choices and limits are listed in `.agents/decisions.md` (Memory notes). Another Memory,
[Memory like Claude Code](2026-11-11-memory-like-claude-code.md), keeps long-term memory
as a folder of files per project instead and can be chosen in its place.

## Alternatives considered

- **Keep the flat list and add a pass after each answer.** Learns more often, but the list
  still grows into every prompt and facts stay one line.
- **Notes loaded by search** (retrieval by similarity before each request). No index for
  the model to see, so it cannot know what it knows; needs an embedding model or a search
  that guesses relevance.
- **A pass in the background after the answer, outside maintain.** The user would never
  wait for it, but the runtime would need a second kind of Memory work outside cycles, and
  Stop, the budget and the per-conversation serialization would need rules for it.
- **Notes shown to the save pass as Markdown headings.** Tried first: Haiku copied a
  heading ("## Kadence: …") as the title of a new note. They are shown in the same tag form
  the pass writes.

## Consequences

- Memory stays small in every request as it grows, and holds more than one-liners.
- Kadence learns from ordinary conversations without being asked: in a check with Claude
  Haiku 5.5, a self-introduction made a profile and a project note, small talk saved
  nothing, and a later plan was appended to the project note, for about one cent over three
  turns.
- One light request after each answer; the agent stays busy ("Tidying up memory…") for a
  few seconds after the answer is shown.
- The pass can save something wrong or miss something; the user sees and edits everything,
  and can tell the agent to correct a note.
- The old `remember` and `forget` tools and the fact list are gone; a store of the old
  schema must be converted by hand.
