# Agent Note: Step descriptions

Status: implemented — every tool call can carry a plain-language sentence the model writes, shown in place of the tool and its input

## Problem

Frontends showed a tool call as its name and raw arguments (`shell · curl -sL -m 20 …`,
`remember {"text": "…"}`). That means nothing to a non-technical user, and a permission
prompt they cannot read gets clicked away. Claude Code shows a short line per step instead.

## Decision

- The runtime adds an optional argument, `api.DESCRIPTION_ARG` (`"description"`), to the
  schema of every tool it offers, built-ins included: "What this step does, in a few plain
  words the user understands without knowing the tool". The module's own spec is not
  changed.
- Before the Tools module sees a call (`confined`, `execute`) the runtime removes it
  (`Runtime._bare`). Events, snapshots and the transcript keep it, so frontends show it
  live and in history. A tool whose own schema declares `description` keeps it as an
  ordinary argument.
- Frontends show the description as the step's title and a permission prompt's headline,
  with the exact input below it or in a tooltip (`frontends.common.call_parts`). Without
  one they show the tool name and its input. No frontend knows any tool by name.
- The automatic permission review leaves the description out of what it judges: the model
  could describe `rm -rf ~/photos` as "tidy up a few files".

## Alternatives considered

- **A fixed sentence per tool in the frontends** ("Search the web for “{query}”"). Built
  first. The frontends had to know the built-in Tools module's tool names, tools from any
  other module got no sentence, and a fixed pattern says what a call touches, not why.
- **A sentence pattern declared in each ToolSpec.** Keeps frontends generic, but every
  module has to write patterns, and patterns still cannot describe complex calls; the
  shell needed a model-written description anyway.
- **A description argument declared by each tool.** How the shell did it at first; every
  module would repeat it, and the module would receive an argument it does not use.
- **A required description.** More consistent titles, but a call without one would fail
  validation and cost a retry; the fallback (name and input) is good enough.

## Consequences

- One mechanism for every tool, present and future, with no tool names in frontends.
- About 10–20 more output tokens per call.
- Models sometimes leave it out, smaller ones more often; those steps show the tool and
  its input.
- Simple steps read wordier ("Read your notes" rather than "Read notes.md"); the exact
  input is one hover away.
- The sentence can mislead. Prompts always show the exact input under it, the docs say to
  trust the input, and the review ignores the sentence.
