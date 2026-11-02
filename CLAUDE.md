# Kayden

Modular AI agent application. Read before coding:
- `SPEC.md`: product requirements (the MVP spec).
- `src/kayden/api.py`: normative module contracts (Core, Memory, Tools, Frontend, RuntimeAPI). Spec section 16 summarizes the behavior it cannot express.

## Stack
Python 3.14 (`requires-python >= 3.12`), asyncio, uv, pytest + pytest-asyncio, ruff. SQLite (stdlib, FTS5) for Memory when it is built.

## Rules
- Define expected behavior as a test before implementing it.
- The runtime owns orchestration; modules never call each other.
- No speculative abstractions, no features outside the MVP, no unrequested refactoring.
- Prefer established libraries over custom mechanisms.
- Modules must never block the event loop.
- Memory archive: append-only, full-fidelity, schema-versioned.
- Each instance writes a JSONL event log of runtime decisions (state changes, tool starts, stops, budget checks).

## Ambiguity
If the spec or `api.py` is ambiguous, contradictory or wrong, do not silently work around it. Record it in `docs/contract-issues.md` (what, where, proposed fix) and continue with the most reasonable reading. Record implementation choices the spec leaves open (storage formats, timeouts, defaults, base system prompt) in `docs/decisions.md`.

## Design docs
Whenever you plan or build a feature or a change of design, write the design to `docs/design/<topic>.md` before implementing: the problem, the chosen design and why, alternatives rejected, and open questions. Update it when the design changes during the work or live results contradict it, so it describes what was built. Small choices still go in `docs/decisions.md`; link the design doc from there.

## Code style
Professional, typed, concise. Comments only where needed to understand the code.

## Commands
- `uv run pytest`
- `uv run ruff check`
