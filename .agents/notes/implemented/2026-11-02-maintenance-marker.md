# Agent Note: Maintenance marker on inference requests

Status: implemented — `InferenceRequest.maintenance` marks Memory's requests (contract issue M14)

## Problem

A Core needs to tell a conversation turn from a Memory maintenance call (compaction
summary, long-term notes), because a provider may need different options for it: the
DeepSeek profile turns thinking off for maintenance so reasoning cannot use up a summary's
token limit (M15). Inferring maintenance from "the request has no tools" ties two unrelated
facts together: a tool-less turn would silently lose thinking, and maintenance with tools
would silently think.

## Decision

- `InferenceRequest.maintenance: bool = False` in `api.py`. The runtime sets it on every
  request made through the `Completion` it hands to Memory (`runtime/orchestrator.py`),
  and nowhere else. SPEC 16.2 states the rule.
- The OpenAI-compatible Core applies its profile's `maintenance_options` when the flag is
  set; user `options` still win over them (see
  [DeepSeek profiles](2026-10-30-deepseek-openai-compatible-profiles.md)).
- The Anthropic Core ignores the field: it has no maintenance-specific options. Per-model
  `options` can still turn thinking off for a dedicated maintenance model.

## Alternatives considered

- **Infer maintenance from a request without tools.** Works only while every turn offers
  built-in tools; couples maintenance detection to tool offering.
- **Pass `light` through instead.** `light` selects the cheaper model; a Completion without
  `light` is still maintenance.

## Consequences

- The runtime's intent is explicit, so neither side relies on how the other uses tools.
- The default of `False` keeps existing Cores and callers valid.
- It is a contract change: every Core sees the field, though only those that need it read it.
