# Concepts

## Modes

Kayden runs in one of two modes, chosen at start.

| | Session mode | Continuous mode |
|---|---|---|
| Like | A normal chat | An always-on assistant |
| Conversations | As many as you like | Exactly one, which keeps going |
| The agent works | Only when you send a message | Until it decides to idle, you stop it, or a limit is hit |

The two modes keep separate conversations and memory. To use both at once, run two Kayden
windows.

## Activation (continuous mode)

When a continuous agent idles, the activation policy decides what wakes it:

- **User-controlled** (default): only you, by sending a message or pressing Ctrl+R.
  Results of background tasks wait for you.
- **Agent-controlled**: it also wakes itself when the background tasks it was waiting for
  finish.

## Background tasks

A tool that runs longer than about 10 seconds moves to the background. The agent gets on
with other work and receives the result when the task ends. It can check on a task or
cancel it, and so can you (Ctrl+K). A task that runs over an hour (unless the tool allows
longer) ends as timed out.

When you stop the agent or quit, running tasks are cancelled. If one does not stop in time,
it is marked as abandoned: it may still be running, and the agent is told to check before
assuming anything.

## Memory

Kayden keeps every message of every conversation permanently.

- When a conversation gets long, older messages are **summarized** so the recent part fits
  the model's context. The originals stay saved.
- The agent can **search** a conversation's full history, including summarized parts.
- **Long-term memory** keeps lasting facts (about you, your setup) across all conversations
  of a mode.
- The agent can **search** the other mode's conversations, never change them.

## Budgets

A budget limits how much Kayden may use: money, tokens, working time or tool calls. Each
limit is a running total for the Kayden data folder and mode. When one is reached, Kayden
stops and refuses to continue until the limit is raised. See [Settings](settings.md).

## Providers and models

A provider is the company whose AI models Kayden uses: Anthropic (Claude), DeepSeek or
OpenAI. Each has several models; larger ones are smarter and cost more. Kayden can use a
cheaper model for memory upkeep than for the conversation itself.
