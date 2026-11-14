# Agent Note: Proposals

Status: implemented — the agent proposes any tool call; the user approves or dismisses it on a card; Kadence runs exactly that call on approval and wakes the agent with the outcome

## Problem

The agent sometimes has an idea the user did not ask for: a side task worth handing to a
sub-session, a cleanup, a follow-up message. Doing it unasked oversteps; asking in prose
leaves the user to type "yes", and nothing holds the agent to doing exactly what was
agreed. The Claude app solves this with cards: the agent proposes, the user clicks, and
only then does the work start.

[Sub-sessions](../implemented/2026-11-11-sub-sessions.md) built that for one tool:
`start_session` had an own-initiative mode that made a proposal in the sidebar. Any other
action had no such path, and every new tool would have needed its own.

Permission prompts are not the same thing. A prompt comes from the user's policy and holds
the call until it is answered; a proposal is the agent's choice to ask first, and nothing
waits on it.

## Decision

### The tool

- `propose(call, reason)`, a runtime built-in offered in every request: `call` is
  `{name, arguments}` of a tool the agent could call now (a Tools module's, the
  frontend's), `reason` one line on why.
  Kadence validates it like a call (known name, arguments match the schema); an invalid one
  is an error result at once.
- The call returns at once with a placeholder, like a backgrounded call: the proposal is an
  operation with the `propose` call's id. The agent carries on or ends its turn.
- On **Approve**, Kadence runs exactly the proposed call: the user's click is its
  permission, so it is not gated again; budgets apply. Its result becomes the operation's
  outcome. On **Dismiss**, the outcome is "dismissed". Either wakes the agent.
- A pending proposal does not hold back other wake-ups: it is left out of the wake set,
  and its outcome wakes the agent by itself.
- `op_cancel` withdraws a proposal; Stop withdraws them all, as it cancels all operations,
  and so does opening another conversation, since a proposal belongs to its conversation.
  A restart ends them as interrupted, like any operation. Waiting does not count toward
  the duration budget, as a permission prompt does not.

### The contract

- `api.Proposal` (id, the proposed `ToolCall`, reason); events `ProposalMade` and
  `ProposalResolved(proposal_id, accepted)` (None: withdrawn); `RuntimeSnapshot.proposals`;
  `RuntimeAPI.resolve_proposal(proposal_id, accept)`. The headless channel and the remote
  runtime carry them.

### Frontends

- A card where permission cards are: "Kadence proposes: <the call's step sentence>", the
  reason, **Approve** and **Dismiss**. The TUI: a prompt line, `/approve` and `/dismiss`.

### The wrap-up card

- An option of a [wrap-up card](2026-11-11-wrap-up-card.md) may carry a call: it is a
  proposal with `card=True` and the `wrap_up` call's id, shown on that card instead of its
  own, and a user message withdraws it.

### Sub-sessions

- `start_session` has no mode of its own: when the user asked, the agent calls it; when it
  is its own idea, it proposes it, and the card shows the task.

Choices are in `.agents/decisions.md` (Proposals).

## Alternatives considered

- **Per-tool proposal modes** (as `start_session` had). Each tool would need its own
  states and UI; the agent's way of asking would differ by tool.
- **A question tool** (`ask_user(question, options)`), with the agent acting on the answer.
  General, but nothing holds the agent to doing what was approved, and it needs another
  turn to act.
- **Permission prompts with a "later" button.** Reuses a card, but a prompt is the policy's,
  holds its call, and blocks other calls behind a barrier; a proposal is the agent's and
  blocks nothing.
- **Proposals kept across restarts** (the Claude app keeps cards a week). Better, but an
  operation never outlives its process; not needed yet.

## Consequences

- Any action can be put to the user the same way, and what runs is exactly what was
  approved; new tools need nothing to support it.
- `start_session` and the sub-session states are simpler.
- **Approval fatigue.** An agent that proposes everything is as tiring as one that asks in
  prose; the tool's description asks it to propose only its own ideas, not what the user
  asked for.
- **Lost on quit, stop and switching.** Pending proposals end with the process, with Stop
  and when another conversation opens.
- Every request carries one more tool schema (about 150 tokens).
