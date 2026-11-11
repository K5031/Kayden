# Agent Note: Memory like Claude Code

Status: implemented — a second Memory module, `kayden.memory.files`, keeps long-term memory as Claude Code does: a folder of Markdown files per project, written by the agent itself

Sits beside [Memory as notes](2026-11-10-memory-notes.md), which stays the built-in Memory.

## Problem

The built-in Memory keeps a profile and topic notes in SQLite, written mostly by a save
pass that a light model runs after every answer. In use it kept the wrong things: a note
on the Kayden project that repeated what the repository says, plus news of the day's work.
A light model reading one exchange cannot tell what is worth keeping.

Claude Code keeps memory differently, and well: one Markdown file per fact, typed, with a
`MEMORY.md` index loaded into every session, written by the main agent itself while it
works under written rules. The owner wants exactly that (2026-11-11), and wants it as a
test of the architecture: keep the old Memory, build the new one as a module of its own,
and swap it in by configuration, starting from an empty store.

## Decision

### A module of its own

`kayden.memory.files` (entry point `files`), chosen with
`[memory] factory = "kayden.memory.files:factory"`. Its archive (messages, summaries,
search, conversation metadata) is the built-in one's, shared through the base class
`SqliteArchive` in `kayden.memory.sqlite`, in its own file, `memory-files.db` (schema 1).
`SqliteMemory`, the built-in Memory, is `SqliteArchive` plus notes, the profile and the
save pass, in `memory.db` (schema 8). A subclass supplies what every request sees of
long-term memory (`_long_term`), what follows an answer (`_after_answer`) and what
deleting a conversation also erases (`_erase`).

### The folder

- One memory folder per project, outside the project:
  `<data dir>/projects/<key>/memory/` (option `projects`), where `<key>` is the
  conversation's folder as an absolute path with `/` turned into `-`
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

The memory rules (Claude Code's: the four types, what not to save, saving in two steps,
`MEMORY.md` as pointers only, check before saving, update or delete wrong memories, link
with `[[name]]`) and then the folder's `MEMORY.md`, from the top within the long-term
budget. Memory files are not loaded; the agent opens one when it is relevant.

### The agent's tool

Claude Code writes memory with its ordinary file tools. Kayden's file tools belong to the
Tools module, and modules never reach into each other's storage, so this Memory offers one
tool on its folder, `memory`, with the commands of Anthropic's memory tool, which Claude
models know: `view`, `create`, `str_replace`, `insert`, `delete`, `rename`. Paths are
relative to the memory folder; anything that leaves it is refused. No pass after an answer
saves anything, and there is no profile: facts about the user are `user` memories.

### The contract

The notes API serves both modules. Each file is a `MemoryNote`: its path is the id and the
title, its frontmatter's description the description, its body the text. The notes methods
take `folder`, the conversation's work folder, which the runtime passes and a module with
one memory for all conversations ignores. `MemoryNote.fixed` marks a note that can only be
emptied (the built-in profile), so frontends no longer know the profile's title.

## Alternatives considered

- **Replace the built-in Memory.** Built first: the store went to schema 9 without notes,
  and the contract got memory-file methods instead of notes. It works, but leaves no
  choice, needs the owner's store converted by hand, and does not show that a Memory can
  be swapped.
- **A Memory written from scratch, without the shared archive.** The purest test, but it
  would copy the archive, search and erasure, which have nothing to do with how long-term
  memory is kept.
- **Let the agent use the Tools module's file tools on the memory folder.** Closest to
  Claude Code, but the Tools module would then write Memory's storage, and the folder sits
  outside the work folder, so every save would meet a permission check.
- **Memory-file methods in the contract beside the notes methods.** Frontends would need
  a second Memory window for the same job; a file maps onto a note without loss.

## Consequences

- Swapping Memory needed, outside the new module: the `folder` keyword and `fixed` in the
  contract, the runtime passing the open conversation's folder, frontends reading `fixed`,
  and the archive split out of `SqliteMemory`. Core, Tools and the runtime's loop are
  unchanged, and the same runtime test passes on both modules.
- Switching starts from an empty archive: the other module's conversations stay in their
  file and come back when switched back.
- Memory is written by the model doing the work, one fact per file; when it forgets to
  save, nothing catches it, as in Claude Code.
- Memory is per project: what Kayden learns in one project is not seen in another.
- The files can be read and edited outside Kayden. Their edits keep no revisions.
