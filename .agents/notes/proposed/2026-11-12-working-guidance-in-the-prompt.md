# Agent Note: Working guidance in the prompt

Status: proposed — a built-in page on how Kayden works, always in the system prompt, with the user's own prompt after it and each trade's guidance in a skill

## Problem

Kayden's built-in system prompt is 509 characters: who it is, how background operations
work, and that `recall` exists. A prompt the user writes replaces it, so the user's
Kayden runs on one sentence of persona. What the agent knows about how to work comes from
the tool descriptions, the Memory module's rules and the model's own habits.

Guidance that belongs to no single tool has nowhere to live. Rules added on 2026-11-12
(plan first, close the plan when finishing a goal, how to answer when the user is
talking) each went into a tool's description or a runtime notice because that was the
only place that survives a custom prompt. Claude Code and Codex carry long prompts on how
to work: when to act and when to ask, checking work before calling it done, reporting
failures plainly, care with what cannot be undone, staying inside what was asked, how to
write the answer.

The gap matters most for a fleet (KFM): an agent that holds a role for weeks with nobody
correcting it depends on its working habits.

## Proposal

Three layers, so that the agent stays general and puts its specialisation on:

| Layer | Says | Lives in |
|---|---|---|
| Base | how to work, in any job | a built-in page, always in the system prompt |
| Trade | how to do one kind of work (coding, writing copy, research) | skills, loaded when the task calls for them |
| Role | what this agent is responsible for | the user's prompt, the project, a duty's instruction (in a fleet, the supervisor's brief) |

- The base page is short and holds only habits that are true for any work. Nothing about
  a trade goes in it: no rules about diffs or tests.
- **Nothing is hardcoded for a case** (the user's rule: it is brittle and does not
  generalize). What an agent should do is reached for in this order: the model's own
  judgment; structure, so that the right behaviour falls out of how Kayden works; one
  general principle, stated once; never a rule for a specific case or a patch for one
  observed mistake. A sentence that makes up for what a model cannot yet do goes stale
  when the model improves, and nobody removes it.
- The user's prompt is added after the base page instead of replacing it.
- Coding guidance becomes the first trade skill.
- The base page is written for Kayden's own tools and tested with real models, the
  smaller ones too, since it has to work across providers.

## Alternatives considered

- **Copy an industry prompt (Codex's).** It is written for another agent's tools (its
  patch tool, sandbox modes, approval rules), tuned to another model family, and it is a
  coding prompt, while Kayden is a general agent and a fleet's teams are marketing or
  support. Its structure and topics are worth taking; its text is not.
- **One long general prompt that also carries coding.** What Claude Code does, and right
  for a tool that only codes. For a general agent every rule is paid for on every request
  by every agent, and a prompt that covers every trade a little is good at none.
- **Keep putting rules in tool descriptions.** Works for a rule about one tool. Rules
  about no tool have no home, and the guidance is scattered where nobody reads it whole.

## Acceptance criteria

- With a custom prompt set, the request's system prompt holds the base page and then the
  user's prompt.
- The base page names no trade.
- A coding task loads the coding skill without being told to.
- A set of repeatable tasks is run before and after: the change is kept only if it does
  not make them worse.

## Risks

- More tokens in every request; the provider's prompt cache makes them cheap after the
  first request of a conversation, not free.
- Too many rules make an agent stiff. Deciding what earns a place is ongoing.
- An agent that fails to load the right skill works as a plain generalist, and nothing
  shows why the result is mediocre.
- A layered generalist will still lose to a purpose-built specialist on its home ground.
- There is no set of repeatable tasks yet: so far prompt wording has been judged by
  reading single runs.
