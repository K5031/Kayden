# Agent Note: Wrap-up card

Status: implemented — when the user has a decision to make, the agent ends its turn with a card: a question, up to three options, one of which may run a call, and an optional recap

## Problem

A turn often ends with a decision for the user: push or not, which fix first,
carry on or stop. The user then scrolls back to see what changed, types "yes", or retypes an
option the agent just listed. The Claude app ends such a turn with one box: a short recap of
what is done and not done, one question, and buttons, the agent's pick first. A button
either sends a reply or runs an action (commit, create a PR) directly, and typing something
else still works. Its own ideas for later are separate cards.

[Proposals](2026-11-11-proposals.md) are Kayden's separate cards: one tool call, Approve or
Dismiss, pending while the agent works on. `ask_for_help` asks a question but offered no
answers to pick.

## Decision

- **A built-in `wrap_up(question, options, recap?)`,** in `BUILTIN_TOOLS`, offered in every
  request, only for a turn that leaves the user a decision. `options` are one to three, each
  a `label` written as the user would say it and, for at most one of them, a `call` to run;
  `recap` is one or two sentences when the decision needs them. The description says to
  call it alone, last.
- **It ends the turn.** When it is the only call in its message, the cycle ends as after an
  answer that calls no tool: no further request is paid for. Called with other tools, it is
  an error result and the cycle goes on.
- **An option with a call is a proposal.** The runtime validates the call as `propose`
  does and starts a `Proposal` with `card=True` and the `wrap_up` call's id; the wrap-up
  result is BACKGROUNDED. Clicking the option approves it (`resolve_proposal`), so exactly
  that call runs ungated and its result wakes the agent. A user message withdraws it first:
  its cancelled outcome is appended before the message, and the request that answers the
  message is the only one.
- **Frontends read the card from the transcript.** `common.card_of(call)` gives a `Card`
  for `wrap_up` and for `ask_for_help` with `replies`. A frontend shows the newest one while
  no user message follows it, so it survives a restart and shows again on reopening. The
  `wrap_up` call is not shown as a step, and a card proposal is not shown as its own card.
- **A click on an option sends its label as an ordinary message,** unless it is the option
  with a call and that proposal is pending: then it approves the proposal. After a restart
  the proposal is gone and the option just sends its label. ✕ puts the card away in that
  window only. Desktop: `WrapUpCard` above the message box with the permission and proposal
  cards, the first option drawn as the main button. Terminal app: `WrapUpPrompt` in the same
  place.
- **Headless clients** see the call and the card proposal like any other;
  `docs/headless.md` says what they mean.

## Alternatives considered

- **Merging `propose` into the card** (one tool for replies, actions and the agent's own
  ideas). One mechanism, but a proposal is pending while the agent works on and several can
  be open, while the card is the end of a turn; one tool would still need both behaviours.
  The Claude app keeps them apart too.
- **A card after every working turn,** recap only when there is no decision, as the Claude
  app does. There the recap is the session's status in a sidebar of many sessions; in
  Kayden the answer right above already says what was done, so the card would repeat it and
  cost output tokens on every working turn.
- **Replies only, no recap or actions** (`suggest_replies`). Smaller, but the user still
  scrolls back for what changed, and "Yes, push" costs a request in which the agent pushes.
- **Markup in the answer's text** (a block frontends strip). No tool and no extra request
  either, but every frontend and headless client must parse and hide it, and a model that
  half-writes the block leaves markup in the answer.
- **A runtime event and snapshot field for the open card.** Explicit, but it duplicates
  what the transcript already holds and needs its own persistence.

## Consequences

- Answering a pending decision is one click, costs no extra request, and an action runs
  exactly as shown.
- Every request carries one more tool schema, about 350 tokens; only turns that end with a
  decision pay for the card's output.
- The model may add a card where none is needed, or write labels as itself ("I'll push").
  The description guards against both; if real use shows it is not enough, the base system
  prompt should say it too.
- Closing the card is per window and not saved: it comes back on reopening.
- A frontend that ignores the card loses only the shortcut: the answer's text still says
  what was done and asks the question.
