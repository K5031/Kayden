# Concepts

## Conversations

Every chat with Kayden is a conversation. You can have as many as you like, go back to any
of them, and start a new one whenever the agent is not busy. Kayden names each
conversation after its first message; you can rename it.

- **New conversation**: a fresh start. The agent sees nothing of your other chats except
  [long-term memory](#memory).
- **Continue from**: a new conversation that knows where an earlier one left off. Kayden
  writes a short summary of the earlier one into the new one, then leaves the earlier one
  as it was. Use it when a conversation has grown long or wandered off.
- **Archive** hides a conversation from the list and from the agent's searches. You can
  bring it back.
- **Delete** erases a conversation for good. See [Deleting](#deleting).

## Background tasks

A tool that runs longer than about 10 seconds moves to the background. The agent gets on
with other work, or tells you it is waiting. When the tasks it started have all ended, the
agent is woken with the results and carries on by itself, so you don't have to ask whether
it is done. It can check on a task or cancel it, and so can you (Ctrl+K). A task that runs
over an hour (unless the tool allows longer) ends as timed out.

That is the only time the agent acts without a message from you. Permissions and the
spending limit apply as always, and after Kayden restarts it waits for you before doing
anything.

When you stop the agent or quit, running tasks are cancelled. If one does not stop in time,
it is marked as abandoned: it may still be running, and the agent is told to check before
assuming anything.

## Memory

Kayden keeps every message of every conversation until you delete the conversation.

- When a conversation gets long, older messages are **summarized** so the recent part fits
  the model's context. Long tool output (command output, web pages) is shortened for the
  summary, so summaries stay cheap. The originals stay saved, in full.
- The agent can **search** a conversation's full history, including summarized parts, and
  your other conversations that are not archived.
- **Long-term memory** is a set of notes shared by all your conversations: **About you**
  (who you are, how you like answers) and one note per project, person or topic, each with
  a one-line description. Every conversation sees About you and the list of notes; the
  agent opens a note when it is relevant, so memory stays cheap as it grows.
- After each answer, Kayden's cheaper upkeep model reads the exchange and saves what will
  matter later (decisions, plans, lasting facts) into the notes; small talk is skipped.
  When it saves something, a line under the answer says so ("Memory updated: Trip"), with
  a link to Memory in the app (F4 in the terminal).
  The agent also saves when you ask it to remember something, and deletes a note when you
  say it is no longer true. It never keeps passwords or keys on purpose. You can read, add,
  edit and **delete** notes in Memory. Notes stay when the conversation that wrote them is
  deleted. Deleting a note does not delete the messages it came from.

## Deleting

Deleting a conversation erases its messages, its summary and its search entries. Memory
notes it wrote stay, so Kayden still knows what it learned; to remove one too, delete it in
Memory. Deleting a note erases it with its earlier versions. Kayden overwrites the erased content in its database file, so it
cannot be recovered from there. It cannot be undone.

Deletion only reaches Kayden's database. It does not reach:

- copies outside Kayden's data folder, such as backups of your computer;
- what the AI provider already received to answer each message; the provider keeps that
  according to its own retention policy.

Kayden's activity log (`events.jsonl` in the data folder) holds nothing to delete: it
records what Kayden did and when, never what was said, what a tool was asked or what it
returned.

To tidy the list without losing anything, archive instead.

## Budgets

A budget limits how much Kayden may use: money, tokens, working time or tool calls. Each
limit is a running total for the Kayden data folder, across all its conversations. When one
is reached, Kayden stops and refuses to continue until the limit is raised. See
[Settings](settings.md).

## Providers and models

A provider is the company whose AI models Kayden uses: Anthropic (Claude), DeepSeek or
OpenAI. Each has several models; larger ones are smarter and cost more. Kayden can use a
cheaper model for memory upkeep than for the conversation itself.
