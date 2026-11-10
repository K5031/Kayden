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

## Autonomy

A conversation is either ordinary (the default) or autonomous. You choose when it starts,
and can still change your mind until you send its first message; after that it stays as it
is. To carry on an ordinary conversation autonomously, continue it in a new one
(**Continue from**) and turn autonomy on there.

| | Ordinary | Autonomous |
|---|---|---|
| Meant for | Working together, step by step | Handing over a task and coming back to the result |
| When it is unsure | Asks you | Decides by itself and tells you what it chose; asks only when a wrong guess would be costly |
| Results of background tasks | Wait for your next message | Wake the agent, which carries on |

An autonomous agent does not act out of nowhere: it works on what you gave it, then
waits until its background tasks finish or you write again. After Kayden restarts, it waits
for you before doing anything.

Autonomy does not change what the agent may do. It still asks before actions as your
[permissions](settings.md#permissions) say, and a question nobody answers stops the work.
For a hands-off run, choose "Read freely, ask before changes" or review what it asks for
before you leave. Spending limits apply as always, so watch the costs of a conversation
left to work on its own.

## Background tasks

A tool that runs longer than about 10 seconds moves to the background. The agent gets on
with other work and receives the result when the task ends. It can check on a task or
cancel it, and so can you (Ctrl+K). A task that runs over an hour (unless the tool allows
longer) ends as timed out.

When you stop the agent or quit, running tasks are cancelled. If one does not stop in time,
it is marked as abandoned: it may still be running, and the agent is told to check before
assuming anything.

## Memory

Kayden keeps every message of every conversation until you delete the conversation.

- When a conversation gets long, older messages are **summarized** so the recent part fits
  the model's context. The originals stay saved.
- The agent can **search** a conversation's full history, including summarized parts, and
  your other conversations that are not archived.
- **Long-term memory** is one list of lasting facts (about you, your setup) shared by all
  your conversations. Kayden adds to it as conversations get summarized. You can see the
  list and add, correct or **forget** entries. A forgotten fact is erased, and Kayden does
  not pick the same fact up again from the summary it came from; it may learn it again
  from new messages, in which case forget it again or tell the agent not to remember it.
  Forgetting a fact does not delete the messages it came from.

## Deleting

Deleting a conversation erases its messages, its summaries, its search entries and the
long-term memory facts that came from it. Forgetting a long-term memory fact erases it with
its earlier versions. Kayden overwrites the erased content in its database file, so it
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
