---
name: consolidate-memory
description: Tidy long-term memory. Merge duplicates, fix or delete what is out of date, drop what is not worth keeping, and trim the index. Use when the user asks to clean up, review or consolidate memory.
---

Review your long-term memory as a whole and leave it smaller, current and correct. Work
only with your memory tools; do not ask the user to do any of it.

1. **Read everything.** Open every memory, not only the index or the titles: with notes,
   `read_note` each one; with a memory folder, `memory` `view` the folder and then each
   file.
2. **Decide for each memory**, one fact at a time:
   - **Delete** what is no longer true, what was only news of past work (what was done,
     the state of a task), what the project's files, docs or git history already say, and
     what will not matter in a later conversation.
   - **Fix** what is partly wrong or out of date. If you cannot tell whether a fact still
     holds and you can check it with your tools, check it; if you cannot, keep it and say
     so at the end.
   - **Merge** memories that say the same thing or belong together into one, and delete
     the others.
   - **Split** a memory that mixes unrelated facts, where your memory keeps one fact each.
   - **Keep** the rest as it is. Do not reword a memory that is already right.
3. **Write the changes** with the same tools (`save_note` and `forget_note`, or `memory`
   `str_replace`, `create`, `delete`, `rename`). Dates stay absolute. Never keep passwords
   or keys.
4. **Fix the index**, where your memory has one (`MEMORY.md`): one line per memory that
   exists, none for one that does not, each a pointer and not the content.
5. **Report** in a few lines: what you deleted, fixed and merged, and anything you were
   unsure about and left for the user to decide.

When arguments are given, review only the memories on that topic.
