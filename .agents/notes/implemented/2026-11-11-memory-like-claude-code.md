# Agent Note: Memory like Claude Code

Status: implemented — a second Memory module, `kadence.memory.files`, keeps long-term memory as Claude Code does: a folder of Markdown files per project, written by the agent itself

Sits beside [Memory as notes](2026-11-10-memory-notes.md), which stays the built-in Memory.

## Problem

The built-in Memory keeps a profile and topic notes in SQLite, written mostly by a save
pass that a light model runs after every answer. In use it kept the wrong things: a note
on the Kadence project that repeated what the repository says, plus news of the day's work.
A light model reading one exchange cannot tell what is worth keeping.

Claude Code keeps memory differently, and well: one Markdown file per fact, typed, with a
`MEMORY.md` index loaded into every session, written by the main agent itself while it
works under written rules. The user wants exactly that (2026-11-11), and wants it as a
test of the architecture: keep the old Memory, build the new one as a module of its own,
and swap it in by configuration, starting from an empty store.

## Decision

### A module of its own

`kadence.memory.files` (entry point `files`), chosen with
`[memory] factory = "kadence.memory.files:factory"`. Its archive (messages, summaries,
search, conversation metadata) is the built-in one's, shared through the base class
`SqliteArchive` in `kadence.memory.sqlite`, in its own file, `memory-files.db` (schema 1).
`SqliteMemory`, the built-in Memory, is `SqliteArchive` plus notes, the profile and the
save pass, in `memory.db` (schema 8). A subclass supplies what every request sees of
long-term memory (`_long_term`), what follows an answer (`_after_answer`) and what
deleting a conversation also erases (`_erase`).

### The folder

- One memory folder per project, outside the project:
  `<data dir>/projects/<key>/memory/` (option `projects`), where `<key>` is the
  project's absolute path with `/` turned into `-` (`-home-k5031-Projects-Kadence`), as
  Claude Code names its project folders. The project is the git repository the
  conversation's work folder is in, the main one when that is a worktree, so every folder
  and worktree of a repository shares one memory; outside a repository it is the work
  folder itself (`folder.project_root`, which reads `.git` and asks no git command). A
  conversation without a folder uses the key `default`.
- In it, `MEMORY.md`, the index, and one `.md` file per memory, in Claude Code's format:

  ```markdown
  ---
  name: <short-kebab-case-slug>
  description: <one-line summary, used to decide relevance>
  metadata:
    type: user | feedback | project | reference
    modified: 2026-11-12T10:20:30.123Z
  ---

  <the fact; for feedback and project, then **Why:** and **How to apply:** lines.
  Related memories linked as [[their-name]].>
  ```

  `modified` is the time the file was written, set on every write by the agent or the
  user (`folder.stamped`), as Claude Code records it.

  `MEMORY.md` holds one line per memory: `- [Title](file.md) — hook`.

### What every request sees

The memory rules (Claude Code's: the four types, what not to save, saving in two steps,
`MEMORY.md` as pointers only, check before saving, update or delete wrong memories, link
with `[[name]]`) and then the folder's `MEMORY.md`, from the top within the long-term
budget. Memory files are not loaded; the agent opens one when it is relevant.

The index is read when a conversation is first assembled in this process and kept for that
conversation, as Claude Code loads memory once per session: what the agent saves meanwhile
is in the conversation itself (its memory calls), and the start of every request stays the
same, so a provider's prompt cache stays valid. It is read again when the conversation's
rolling summary changes, since the prompt changes then anyway. A change made elsewhere
(the Memory window, another conversation) reaches an open conversation only then, or when
Kadence starts again.

### The agent's tool

Claude Code writes memory with its ordinary file tools. Kadence's file tools belong to the
Tools module, and modules never reach into each other's storage, so this Memory offers one
tool on its folder, `memory`, with the commands of Anthropic's memory tool, which Claude
models know: `view`, `create`, `str_replace`, `insert`, `delete`, `rename`. Paths are
relative to the memory folder; anything that leaves it is refused. No pass after an answer
saves anything, and there is no profile: facts about the user are `user` memories.

### What keeps it from going wrong

The layout is the smaller part. An agent that writes its own memory can save a wrong
fact, turn one remark into a trait, or leave an entry to go stale; what holds that in
check are the limits on writing, as in Claude Code, where its file tools refuse to change
a file that was not read:

- **Rules in every request** for what may be saved and what may not.
- **Read before write.** The tool changes, deletes or renames a file only if this
  conversation has read it and it is still as it was read (`tool.run_command`, `seen`): a
  memory written by another conversation or by the user is never altered unseen, and one
  that changed meanwhile is read again. `MEMORY.md` counts as read while it is what the
  conversation's prompt shows.
- **Small edits.** `create` writes only a new file; an existing one is changed with
  `str_replace` or `insert`.
- **An index that has to stay short.** As in Claude Code, a conversation is given the
  first 200 lines or 25 kB of `MEMORY.md`, whichever ends first (`folder.loaded_index`).
  A write that takes the index past that stands, but the tool answers with an error
  telling the agent to rewrite it, since what lies beyond is not seen again; from 80 %
  of either limit a write comes with a reminder to keep one line per memory, move detail
  into the memory files and merge or drop stale entries. A memory file has no limit of
  its own.
- **Everything is visible.** The Memory window shows every file, and they are plain files.

### The contract

The notes API serves both modules. Each file is a `MemoryNote`: its path is the id and the
title, its frontmatter's description the description, its body the text. The notes methods
take `folder`, the conversation's work folder, which the runtime passes and a module with
one memory for all conversations ignores. `MemoryNote.fixed` marks a note that can only be
emptied (the built-in profile), so frontends no longer know the profile's title.

## Alternatives considered

- **Replace the built-in Memory.** Built first: the store went to schema 9 without notes,
  and the contract got memory-file methods instead of notes. It works, but leaves no
  choice, needs the user's store converted by hand, and does not show that a Memory can
  be swapped.
- **Refuse a write that takes the index past its limit, and cap each memory file.** Built
  first (200 lines, 4000 characters). Stricter than Claude Code, which lets the write
  stand and reports it, and has no limit per file; the user wants it the same.
- **Topic files that group related memories.** One file per memory is what Claude Code
  does ("one topic file per memory", one index line each): a single fact can be corrected
  or deleted without touching others, and files do not grow into pages.
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
- Checked with Opus 5.5 in a git repository: a memory saved from a chat in a subfolder
  landed in the repository's memory folder with its `modified` time, and a chat at the
  repository's root answered from it.
- The guards cost a tool call: to change a memory the agent first views it. Checked with
  Opus 5.5: asked to update a memory another conversation wrote, it viewed the file, then
  replaced one sentence.
- An index left over its limit keeps its tail on disk but out of every conversation, until
  the agent rewrites it.
- Memory is per project: what Kadence learns in one project is not seen in another. A
  project is a whole repository, so a chat in a subfolder or a worktree shares its memory.
- The files can be read and edited outside Kadence. Their edits keep no revisions.
