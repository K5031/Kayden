# Agent Note: DeepSeek through OpenAI-compatible profiles

Status: implemented — the OpenAI-compatible Core reads provider differences from a `Profile`

## Problem

DeepSeek speaks the chat completions API, so `kayden.core.openai_compat` almost works
against it, but not reliably:

- Thinking mode requires the reasoning of earlier assistant turns to be sent back in a
  `reasoning_content` field; stock chat completions has no such field, so requests fail
  with 400.
- The key and base URL differ, and a DeepSeek instance must not pick up or send the user's
  OpenAI credentials (`OPENAI_API_KEY`, org and project headers).
- Thinking counts against `max_tokens`, and `/models` reports a 393216-token output limit,
  so a summary request can end with reasoning only and no text.
- DeepSeek has its own transient finish reasons, and prices that `/models` doesn't report.

Constraints: modules are self-contained, and generic Core code has no provider-name
branches.

## Decision

A `Profile` is plain data describing what a server does differently from stock chat
completions. Generic code reads its fields and never a provider name. The factory option
`profile` selects one; `"openai"` (the default) is stock behavior.

| Field | `deepseek` |
|---|---|
| `base_url` | `https://api.deepseek.com`; with it set, nothing is read from the OpenAI environment and the org/project headers are suppressed |
| `api_key_env`, `api_key_required` | `DEEPSEEK_API_KEY`, required at construction |
| `models` | `deepseek-flash`, `deepseek-v4-pro`: `max_output_tokens` 64000 and peak prices |
| `transient_finish_reasons` | `insufficient_system_resource`, `aborted` → `TransientCoreError` |
| `reasoning_input_field` | `reasoning_content` |
| `maintenance_options` | `{"thinking": {"type": "disabled"}}` |

- **Reasoning replay.** Every assistant message carries the field on every request: the
  same model's reasoning, else `""`. Reasoning from another model is never replayed (M10).
- **Maintenance requests.** A request with `InferenceRequest.maintenance` set runs with
  `maintenance_options`: thinking off, so a summary can't be all reasoning (see
  [maintenance marker](2026-11-02-maintenance-marker.md)). User `options` override the
  profile.
- **Model config.** User `models` entries merge over the profile's, field by field, so
  overriding a context window keeps the prices.

Live results: `docs/decisions.md`, "OpenAI-compatible profiles".

## Alternatives considered

- **A separate DeepSeek Core module.** Duplicates nearly all of the OpenAI-compatible Core
  for a handful of differences.
- **`if provider == "deepseek"` branches.** Ruled out by the constraints; differences would
  scatter through the code.
- **Sending the reasoning field only on tool-call turns** (what DeepSeek's docs suggest).
  Live, requests without tools also failed with thinking on.
- **Omitting the field when no reasoning is known.** Live, an omitted field is rejected
  whenever the server no longer has the call's reasoning; `""` was accepted everywhere tried.
- **Changing `_common.py` so configured prices survive.** Left alone on request; merging
  happens inside `openai_compat.py`.

## Consequences

- Every provider difference sits in one visible place, and the generic path stays testable
  with the stock profile.
- `max_output_tokens` is 64000 (DeepSeek's thinking default) instead of the reported
  393216, which would let the runtime reserve a quarter of the window for output.
- One price per model (M13): DeepSeek's off-peak price is half the peak price, so costs
  read up to 2x high and cost budgets stop early.
- Setting `thinking` in the Core's `options` turns thinking back on for maintenance and
  re-exposes empty summaries (M15).
- Prices are dated 2026-10-30 and nothing refreshes them.
- `insufficient_system_resource`, `aborted` and `tool_choice` with thinking were not seen
  live.
