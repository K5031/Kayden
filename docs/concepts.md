# Concepts

## Conversations

Every chat with Kadence is a conversation. You can have as many as you like, go back to any
of them, and start a new one whenever the agent is not busy. Kadence names each
conversation after its first message; you can rename it.

- **New conversation**: a fresh start. The agent sees nothing of your other chats except
  [long-term memory](#memory).
- **Continue from**: a new conversation that knows where an earlier one left off. Kadence
  writes a short summary of the earlier one into the new one, then leaves the earlier one
  as it was. Use it when a conversation has grown long or wandered off.
- **Archive** hides a conversation from the list and from the agent's searches. You can
  bring it back.
- **Delete** erases a conversation for good. See [Deleting](#deleting).

The agent sees when each of your messages was sent, so it knows the date and time and how
long ago you last wrote. The conversation shows your messages as you typed them.

## Background tasks

A tool that runs longer than about 10 seconds moves to the background. The agent gets on
with other work, or tells you it is waiting. When the tasks it started have all ended, the
agent is woken with the results and carries on by itself, so you don't have to ask whether
it is done. It can check on a task or cancel it, and so can you (Ctrl+K). A task that runs
over an hour (unless the tool allows longer) ends as timed out.

Apart from a [duty](#duties), that is the only time the agent acts without a message from
you. Permissions and the spending limit apply as always, and after Kadence restarts it
waits for you before doing anything.

When you stop the agent or quit, running tasks are cancelled. If one does not stop in time,
it is marked as abandoned: it may still be running, and the agent is told to check before
assuming anything.

## Duties

A duty is standing work you give a conversation, such as "keep my inbox answered". The
agent then carries it on by itself: it keeps a plan of steps, works through them, and
checks in again on its own. You can give any conversation a duty at any time, change it,
or turn it off; the conversation keeps everything said so far.

- **Keep doing it** runs until you turn it off. **Until it is done** also ends when the
  agent says the goal is reached, with a short report.
- **The plan** is a list of steps, each to do, doing, waiting for you, done or dropped, with an optional
  due time and note. The agent always has it in view, and you can edit it. Any
  conversation can have one (see [The plan](using-kadence.md#the-plan)), and a step that
  falls due wakes the agent, with or without a duty (see
  [Reminders](using-kadence.md#reminders)).
- **When it wakes:** when a background task finishes (the first one, not all of them),
  when a step falls due, when you write, and otherwise at least every so often (an hour
  unless you change it). It never checks in more often than the minimum you set (a
  minute unless you change it); a finished task or your message wakes it at once.
  To react to the outside world, the agent starts a tool that waits for something, for
  example new mail through a tool server; when it finishes, the agent wakes.
- **A spending limit is up to you.** A duty runs without one. Since it can work for days,
  setting one in Settings caps what it can cost; your provider's own spend limit is the
  other guard.
- **It pauses** when you press Stop, when the limit is reached, after an error, and after
  Kadence restarts, so a crash never quietly repeats an action. Send a message or activate
  the conversation (Ctrl+R) to resume it.
- **It asks when it is stuck.** When only you can unblock it (a decision, an approval,
  access it lacks), the agent asks you a question and stops checking in until you
  answer. The question appears in the conversation and the status line says the duty is
  waiting for you. Your reply resumes it; finished background tasks still wake it
  meanwhile.
- **Permissions still apply.** An action that would ask you waits for you, so a duty that
  runs unattended needs permissions that cover what it does.
- **Continue from** does not copy a duty.

The status line says when the duty checks in next, or that it is paused.

## Sub-sessions

The agent can hand a self-contained task to a sub-session: a separate agent with its own
context, working in the same folder with the same tools and settings. It runs in the
background while the conversation goes on, and its report comes back as the result of that
step. Several can run at once.

- **You decide on the agent's own ideas.** When you asked for the work, the sub-session
  starts at once. When it is the agent's idea, it is a
  [proposal](using-kadence.md#proposals) you approve or dismiss.
- **The agent hears back.** When a sub-session stops working (it is done, asks something,
  or answered you) and the agent was not already waiting for it, the agent gets a short
  notice in its conversation, which wakes it.

- **A spending limit is up to you.** A sub-session has none unless you set a limit per
  sub-session in Settings, or the agent gives one a limit of its own. With yours set, a
  sub-session gets that much unless the agent gives it less, and never more; it is not
  counted in your own limit. To change or remove one sub-session's limit, even after it
  ran out, right-click it: **Change spending limit…** (0 for none), then **Resume** it.
- **It asks you for approvals.** A step that needs your OK waits for you, as in your own
  conversation; the sidebar says the sub-session needs your approval. If it needs a
  decision, its question comes back to the agent, which can answer it or ask you.
- **You can open it.** The desktop app lists each conversation's sub-sessions under it in
  the sidebar. Click one to open it in place of the conversation: its messages and steps,
  its approvals, Stop, and a message box that talks to it. Click the conversation to go
  back; the sub-session keeps working. In the terminal app, `/sessions` does the same.
- **It is kept.** Quitting Kadence stops sub-sessions that are working, but they stay listed,
  and **Resume** lets one carry on with its task. Kadence never resumes one by itself after a
  restart, so a crash cannot quietly repeat an action. Opening one that is not running
  starts it again on its own conversation. Their work is in `sessions/` in the data folder.
- **Right-click one** to open, resume, stop, rename, archive or delete it.
- **They go with their conversation.** Deleting a conversation stops and deletes its
  sub-sessions too; archiving it hides them with it.

## Memory

Kadence keeps every message of every conversation until you delete the conversation.

- When a conversation fills about 90% of the model's context, its older half is
  **summarized** so the recent part fits. Long tool output (command output, web pages) is
  shortened for the summary, so summaries stay cheap. The originals stay saved, in full.
- The summary ends with a list of the **topics covered so far**, one line each. Details
  fade from the summary as it is rewritten, but the list keeps every topic, so the agent
  knows what it can search for. The chat shows
  "Compacting conversation…" while this runs, then "Conversation compacted".
- The agent can **search** a conversation's full history, including summarized parts, and
  your other conversations that are not archived.
- **Long-term memory** is a set of notes shared by all your conversations: **About you**
  (who you are, how you like answers) and one note per project, person or topic, each with
  a one-line description. Every conversation sees About you and the list of notes; the
  agent opens a note when it is relevant, so memory stays cheap as it grows.
- After each answer, Kadence's cheaper upkeep model reads the exchange and saves what will
  matter later (decisions, plans, lasting facts) into the notes. It skips small talk, news
  of recent work and anything Kadence can read again in your files, and rewrites a note that
  has gone out of date, so you do not have to tidy memory yourself.
  When it saves something, a line under the answer says so ("Memory updated: Trip"), with
  a link to Memory in the app (F4 in the terminal).
  The agent also saves when you ask it to remember something, and deletes a note when you
  say it is no longer true. It never keeps passwords or keys on purpose. You can read, add,
  edit and **delete** notes in Memory. Notes stay when the conversation that wrote them is
  deleted. Deleting a note does not delete the messages it came from.
- Kadence can keep long-term memory **per project, as files** instead, the way Claude
  Code does; see [Installed modules](settings.md#installed-modules).

## Skills

A skill is a set of written instructions for one kind of task, which the agent reads only
when that task comes up. Every conversation sees the list of skills (a name and one line
each), so having many costs little.

- **Use one** by asking for the task in your own words, or by writing its name as a
  message: `/consolidate-memory`. Anything after the name is passed to the skill.
- **Kadence comes with** `consolidate-memory`: the agent rereads its long-term memory,
  merges duplicates, fixes or deletes what is out of date and tells you what it changed.
  It runs only when you ask.
- **Add your own** as a folder with a `SKILL.md` file:

  ```markdown
  ---
  name: release-notes
  description: Write release notes from the git log, in this project's format.
  ---

  1. Read the commits since the last tag...
  ```

  The name is lowercase letters, digits and hyphens. Put the folder in `~/.kadence/skills/`
  to have the skill everywhere, or in a project's `.claude/skills/` to have it in chats
  that work in that folder. A project's skill replaces yours of the same name, and yours
  replaces Kadence's. The format is Claude Code's, so its skills work as they are. A new or
  changed skill is picked up with your next message.
- A project's skills are instructions from that project, so open only folders you trust.

## Deleting

Deleting a conversation erases its messages, its summary and its search entries. Memory
notes it wrote stay, so Kadence still knows what it learned; to remove one too, delete it in
Memory. Deleting a note erases it with its earlier versions. Kadence overwrites the erased content in its database file, so it
cannot be recovered from there. It cannot be undone.

Deletion only reaches Kadence's database. It does not reach:

- copies outside Kadence's data folder, such as backups of your computer;
- what the AI provider already received to answer each message; the provider keeps that
  according to its own retention policy.

Kadence's activity log (`events.jsonl` in the data folder) holds nothing to delete: it
records what Kadence did and when, never what was said, what a tool was asked or what it
returned.

To tidy the list without losing anything, archive instead.

## Budgets

A budget limits how much Kadence may use: money, tokens, working time or tool calls. Each
limit is a running total for the Kadence data folder, across all its conversations. When one
is reached, Kadence stops and refuses to continue until the limit is raised. See
[Settings](settings.md).

## Providers and models

A provider is the company whose AI models Kadence uses: Anthropic (Claude), DeepSeek or
OpenAI. Each has several models; larger ones are smarter and cost more. Kadence can use a
cheaper model for memory upkeep than for the conversation itself.
