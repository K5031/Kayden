# Kayden

Modular AI agent application. Read before coding:
- `PHILOSOPHY.md`: where Kayden is going: the base unit of a fleet (KFM) that works without supervision.
- `SPEC.md`: product requirements: what Kayden does.
- `src/kayden/api.py`: normative module contracts (Core, Memory, Tools, Frontend, RuntimeAPI). Spec section 16 summarizes the behavior it cannot express.

## Stack
Python 3.14 (`requires-python >= 3.12`), asyncio, uv, pytest + pytest-asyncio, ruff. SQLite (stdlib, FTS5) for the Memory archive.

## Rules
- Define expected behavior as a test before implementing it.
- The runtime owns orchestration; modules never call each other.
- No speculative abstractions, no features the owner has not asked for, no unrequested refactoring.
- No migration or compatibility code for older data folders, settings or flags. A store of another schema version is refused.
- Prefer established libraries over custom mechanisms.
- Modules must never block the event loop.
- Memory archive: append-only, full-fidelity, schema-versioned. The one exception is deletion the user explicitly asks for.
- Each instance writes a JSONL event log of runtime decisions (state changes, tool starts, stops, budget checks).

## Ambiguity
If the spec or `api.py` is ambiguous, contradictory or wrong, do not silently work around it. Record it in `.agents/contract-issues.md` (what, where, proposed fix) and continue with the most reasonable reading. Record implementation choices the spec leaves open (storage formats, timeouts, defaults, base system prompt) in `.agents/decisions.md`.

## Documentation
`docs/` is the user wiki: concise, plain language, organized by what a reader wants to do. When a change alters user-facing behavior (commands, keys, settings, defaults, limits), update the affected page in the same change. Working records (Agent Notes, contract issues, decisions, plans) go in `.agents/`, never in `docs/`.

## Agent Notes
Design decisions live in `.agents/notes/` as **Agent Notes**: the why and what was given up, which code and docs can't carry.

### When to write one
- Only for lasting decision rationale that code, tests and docs don't explain. Skip mechanical or local changes; small choices go in `.agents/decisions.md`.
- For a new feature or a change of approach, write the proposed note before coding.
- If a note already owns the decision, update it. Don't duplicate.
- Before changing architecture, adding a dependency or switching approach, search the notes. If the change contradicts an accepted decision, flag it and ask.

### Layout
`.agents/notes/{status}/yyyy-mm-dd-topic-title.md`, where the date is when the topic was first proposed.
- `proposed/`: not built yet.
- `implemented/`: shipped. Kept current with the code.
- `rejected/`: declined. Keep only if it prevents a tempting mistake, otherwise delete.

The folder is the status. Moving a note means updating its `Status:` line, rewriting it to the new skeleton, and fixing every link to it, in the same change. Link between notes with relative markdown links. No index file.

### Format
```markdown
# Agent Note: <title>

Status: proposed | implemented | rejected — <one-line reason>

## Problem
## Decision          (proposed: "## Proposal")
## Alternatives considered
## Consequences      (proposed: "## Acceptance criteria" and "## Risks")
```
- `## Problem` is the motivation, readable without the solution.
- `## Decision` is present tense and describes what shipped.
- `## Alternatives considered` is mandatory: each real alternative and why it lost. Never invent alternatives.
- `## Consequences` records what the trade-off cost and what it bought.
- Add bespoke technical sections between these as needed.

### Keeping notes current
- When code changes a fact an implemented note states (path, name, default), update the note in the same change. Rewrite in place, don't append history.
- Never edit a note into a different decision. Write a new note, link both ways, and set the old one's status accordingly.
- State current facts only. No "previously" or "no longer", no commit or PR history.

## Layout
Each module is a folder named after it in the folder for its kind (`src/kayden/<kind>/<module>/`, `__init__.py` exports `factory`). Code shared by several modules of a kind sits in the kind folder, not in a module. See `docs/development.md#layout`.

## Code style
Professional, typed, concise. Comments only where needed to understand the code.

## Commands
- `uv run pytest`
- `uv run ruff check`
