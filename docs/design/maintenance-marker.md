# Maintenance marker on inference requests

Status: built (2026-11-02). Resolves contract issue M14. Code: `api.py` (`InferenceRequest`),
`runtime/orchestrator.py` (`Completion`), `core/openai_compat.py`.

## Problem

A Core could not tell a turn from a Memory maintenance call (compaction summary, long-term
notes). The DeepSeek profile needs to: maintenance runs with thinking off, so reasoning
cannot use up a summary's token limit (M15). The Core inferred "no tools = maintenance",
which held only because the runtime offers built-ins on every turn. A test guarded the
inference, but it tied two unrelated facts together: a tool-less turn would silently lose
thinking, and maintenance with tools would silently think.

## Design

- `InferenceRequest.maintenance: bool = False`. The runtime sets it on every request made
  through the `Completion` it hands to Memory, and nowhere else.
- The OpenAI-compatible Core's profile field `toolless_options` becomes
  `maintenance_options`, applied when `request.maintenance` is true. User `options` still
  win over it.
- The Anthropic Core ignores the field: it has no maintenance-specific options.

## Why

An explicit field states the runtime's intent, so neither side relies on how the other
uses tools. A default of `False` keeps existing Cores and callers valid.

## Rejected

- **Keep the inference.** Works today, but couples maintenance detection to tool offering.
- **Pass `light` through instead.** `light` selects the cheaper model; a Completion without
  `light` is still maintenance.

## Open questions

None.
