# Clean-room build: the Kayden runtime from the spec alone

This is a spec test. You get only the written spec and contract, and build the runtime from them. Where they are ambiguous, the result will show it.

## Inputs
Allowed: `AGENTS.md`, `SPEC.md`, `src/kayden/api.py`, and what you create.
Not allowed: any other directory on this machine (especially anything named `kayden-*` or `Kayden`), other repositories' git history, or web searches for existing Kayden code. If you find such material, ignore it and say so in your report.

## Build
1. `src/kayden/runtime.py` (a package if it grows): the complete runtime. Implement everything `SPEC.md` assigns to the runtime and everything in the `api.py` module docstring and `RuntimeAPI`: both modes, activation policies, `idle()` and its wake set, background operations (placeholder plus `OperationResult`), Stop and abandon, `cancel_operation`, retries and backoff, `ContextOverflowError` recovery, budgets with persistence, permission policies, the ceiling environment variable, resume repair, the instance lock, the event stream and snapshot, the JSONL event log, the built-in tools, and tool-call validation.
2. Fakes in `tests/fakes/`: a scripted Core (text, tool calls, reasoning, usage, transient and permanent errors, slow or hanging streams), Tools (instant, slow with progress, hanging with and without honoring cancellation, failing, thread-using), an in-memory Memory that outlives instances and implements the full contract (trivial compaction through `Completion`), and a scripted Frontend that records every event envelope.
3. Tests, written first:
   - one acceptance test per scenario A to L in `SPEC.md` section 12;
   - one test per runtime guarantee in the `api.py` module docstring;
   - one test per cell of the command × state table in `RuntimeAPI`;
   - budget semantics from `SPEC.md` section 9.

## Process
1. Read the three input files completely.
2. Write `.agents/plan.md`: the list of behaviors you extracted from the spec, each with the test that will cover it.
3. Write the tests, then the runtime. Run `uv run pytest` and `uv run ruff check` until clean.
4. Work in small commits.

## Rules
Follow `AGENTS.md`. Do not modify `api.py` or `SPEC.md`; propose changes in your report.

## Report
- `.agents/contract-issues.md`: every place the spec or `api.py` was ambiguous, contradictory or silent, and what you assumed. Be exhaustive; this is the main output.
- `.agents/decisions.md`: choices the spec leaves to the implementer (defaults, formats).
- Finish with a short summary: tests passing, anything unfinished, and the five ambiguities that cost you the most time.
