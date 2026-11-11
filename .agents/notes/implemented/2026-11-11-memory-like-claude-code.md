# Agent Note: Memory like Claude Code

Status: implemented — long-term memory is a folder of Markdown files per project, written by the agent itself, as in Claude Code

Replaces [Memory as notes](../rejected/2026-11-10-memory-notes.md).

## Problem

Long-term memory is a profile and topic notes in SQLite, written mostly by a save pass
that a light model runs after every answer. In use it kept the wrong things: a note on the
Kayden project that repeated what the repository says, plus news of the day's work. A
light model reading one exchange cannot tell what is worth keeping, nothing cleans up
afterwards, and a topic note grows into a page that is hard to correct one fact at a
time.

Claude Code keeps memory differently, and well: one Markdown file per fact, typed, with a
`MEMORY.md` index loaded into every session, written by the main agent itself while it
works under written rules (save what the repository does not record, check for an existing
memory first, update or delete what turns out wrong). The owner wants exactly that
(2026-11-11): "exact same so if we need to drop sqlite do it".

## Decision

### The folder

- One memory folder per project, outside the project: `<data dir>/projects/<key>/memory/`
  (`kayden.memory.folder`),
  where `<key>` is the conversation's folder as an absolute path with `/` turned into `-`
  (`-home-k5031-Projects-Kayden`), as Claude Code names its project folders. A
  conversation without a folder uses the key `default`.
- In it, `MEMORY.md`, the index, and one `.md` file per memory, in Claude Code's format:

  ```markdown
  ---
  name: <short-kebab-case-slug>
  description: <one-line summary, used to decide relevance>
  metadata:
    type: user | feedback | project | reference
  ---

  <the fact; for feedback and project, then **Why:** and **How to apply:** lines.
  Related memories linked as [[their-name]].>
  ```

  `MEMORY.md` holds one line per memory: `- [Title](file.md) — hook`.

### What every request sees

The system prompt gets, from Memory's addendum, the memory rules (Claude Code's: the four
types and what each is for, what not to save, how to save in two steps, keep `MEMORY.md`
to pointers, check before saving, update or delete wrong memories, link with `[[name]]`)
and then the folder's `MEMORY.md`, cut at the long-term budget. Memory files are not
loaded; the agent opens one when it is relevant.

### The agent's tool

Claude Code writes its memory with its ordinary file tools. Kayden's file tools belong to
the Tools module and its work folder, and modules never reach into each other's storage,
so Memory offers one tool on its folder, `memory` (`kayden.memory.tools`), with the
commands of Anthropic's memory tool, which Claude models know: `view` (a file, or the
folder's listing), `create`, `str_replace`, `insert`, `delete`, `rename`. Paths are
relative to the memory folder; anything that leaves it is refused. The agent keeps
`MEMORY.md` itself, as in Claude Code.

### What is not there

- No pass after an answer saves anything: the runtime still calls `maintain` then, for the
  title and compaction only, and announces no memory changes of its own.
- No profile: facts about the user are `user` memories like any other.
- No revisions, forgotten-fact fingerprints or source conversations.

### The contract

Memory's optional `memory_files(folder)` (`api.MemoryFile`: path, text, updated_at;
`MEMORY.md` first), `write_memory_file(folder, path, text)` and
`delete_memory_file(folder, path)` work on a project's folder, given as the conversation's
work folder; `RuntimeAPI` has the same without the folder, for the open conversation.
`MemoriesChanged` follows every write by the tool or the user. The Memory window in both
apps lists the open conversation's memory files under the name and description of their
frontmatter (`common.memory_label`) and edits a file's text as it is.

### Storage

The SQLite store keeps the archive, summaries and conversation metadata: schema 9, without
the note tables. A store of schema 8 is refused, as for every schema change; the owner's
store is converted once, by hand, its notes written out as memory files.

The prompt carries the rules whenever they fit and `MEMORY.md` from the top within the
long-term budget (5 % of the context).

## Alternatives considered

- **Keep notes in SQLite, shaped like Claude Code's** (a type at the top of each note's
  text, an index from titles and descriptions). No schema change, but not the same: no
  files to read or edit outside Kayden, and the owner asked for the same.
- **Let the agent use the Tools module's file tools on the memory folder.** Closest to
  Claude Code's own tools, but the Tools module would then write Memory's storage, and the
  folder sits outside the work folder, so every save would meet a permission check.
- **Keep the save pass as a safety net.** Catches what the agent forgets, but it is what
  saved the wrong things, and Claude Code has none.
- **One memory for all projects.** Simpler, but Claude Code keeps memory per project, and
  a project's facts are noise in another's prompt.

## Consequences

- Memory is written by the model doing the work, with the conversation in view, and holds
  one fact per file, each with its type and reason: precise to update or delete.
- It costs a tool call on the main model when the agent saves, instead of a light-model
  request after every answer.
- Memory depends on the agent remembering to save; when it forgets, nothing catches it.
  That is Claude Code's trade-off too.
- Memory is per project: what Kayden learns about the user in one project is not seen in
  another, as in Claude Code.
- The files can be read and edited outside Kayden.
- The owner's existing notes had to be converted by hand.
