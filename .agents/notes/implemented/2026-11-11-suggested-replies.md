# Agent Note: Suggested replies

Status: implemented — the agent ends an answer with up to three replies the user can click

## Problem

An answer often ends with a decision for the user: push or not, which fix first, carry on
or stop. The user then types "yes" or retypes an option the agent just listed. The Claude
app puts buttons under such an answer: one question, a few replies written for the user,
the agent's pick first. A click sends that reply as the user's message, and typing
something else still works.

[Proposals](2026-11-11-proposals.md) are narrower: they are about one tool call, and the
only answers are Approve and Dismiss. `ask_for_help` asks a question but offered no answers
to pick.

## Decision

- **A built-in `suggest_replies(replies)`,** in `BUILTIN_TOOLS`, offered in every request:
  one to three replies of at most 80 characters, in the user's words, the recommended one
  first. The question itself is in the answer's text. The description says to use it only
  when the answer leaves the user a decision, gives example replies, and says to call it
  alone, last.
- **It ends the turn.** When it is the only call in its message, the runtime records its
  result and ends the cycle as it does after an answer that calls no tool: no further
  request is paid for. Called with other tools in one message, it is an error result that
  tells the model to call it alone after its work, and the cycle goes on.
- **`ask_for_help` takes optional `replies`** with the same schema, so a supervisor (a
  person or the KFM) can answer a stuck agent with a click.
- **Frontends read the replies from the transcript.** `api.SUGGESTION_TOOLS` names the two
  calls and `common.replies_of(call)` reads their replies. A frontend shows the replies of
  the newest such call while no user message follows it, so they survive a restart and
  show again when a conversation is reopened. No event, snapshot field or storage was
  added. The `suggest_replies` call is not shown as a step.
- **A click sends the reply as an ordinary message;** sending or queueing anything removes
  the buttons, and so does a runtime notice, since the agent is about to go on. Desktop:
  `ReplyButtons` above the message box, with the permission and proposal cards, the first
  drawn as the main button. Terminal app: `ReplyButtons` in the same place, the first a
  primary button.
- **Headless clients** see the call in events and history like any other; `docs/headless.md`
  says what it means.

## Alternatives considered

- **Markup in the answer's text** (a `<replies>` block frontends strip). No tool and no
  extra request either, but every frontend and every headless client must parse and hide
  it, and a model that half-writes the block leaves markup in the answer.
- **A tool that does not end the turn,** like `ask_for_help`. Simpler in the runtime, but
  every suggestion would cost one more model request whose answer is usually empty.
- **Extending proposals with free answers.** One mechanism fewer, but a proposal runs a
  tool call on approval and wakes the agent with its result; a reply is just a message.
  Merging them would make both harder to explain.
- **A runtime event and snapshot field for the open suggestion.** Explicit, but it
  duplicates what the transcript already holds and needs its own persistence.

## Consequences

- Answering a pending decision is one click, and costs no extra request.
- Every request carries one more tool schema, about 150 tokens.
- The model may offer buttons where none are needed, or write replies as itself ("I'll
  push") instead of as the user. The description guards against both; if real use shows
  it is not enough, the base system prompt should say it too.
- A frontend that ignores `SUGGESTION_TOOLS` loses nothing but the shortcut: the question
  is in the answer's text.
