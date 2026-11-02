# DeepSeek through OpenAI-compatible profiles

Status: built (2026-10-30). Code: `src/kayden/core/openai_compat.py`. Live results:
`decisions.md`, "OpenAI-compatible profiles".

## Problem

DeepSeek speaks the chat completions API, so `kayden.core.openai_compat` almost works
against it, but not reliably:

- Thinking mode requires the reasoning of earlier assistant turns to be sent back in a
  `reasoning_content` field; stock chat completions has no such field, so requests fail
  with 400.
- The key and base URL differ, and a DeepSeek instance must not pick up or send the
  user's OpenAI credentials (`OPENAI_API_KEY`, org and project headers).
- Thinking counts against `max_tokens`, and `/models` reports a 393216-token output limit,
  so a summary request can end with reasoning only and no text.
- DeepSeek has its own transient finish reasons and prices that `/models` doesn't report.

Constraints: modules are self-contained, `api.py` is not changed, and generic Core code
has no provider-name branches.

## Design

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
- **Maintenance requests.** A request marked `maintenance` runs with `maintenance_options`:
  thinking off, so a summary can't be all reasoning. (Until M14 was resolved the Core
  inferred maintenance from a request having no tools; see `maintenance-marker.md`.) User `options` override the profile.
- **Model config.** User `models` entries merge over the profile's field by field, so
  overriding a context window keeps the prices.

## Why

- Plain data keeps every provider difference in one visible place and keeps the generic
  path testable with the stock profile unchanged.
- `""` instead of omitting the field: live, an omitted field is rejected whenever the
  server no longer has the call's reasoning, while `""` was accepted everywhere tried.
- 64000 output tokens is DeepSeek's thinking default; the reported 393216 would let the
  runtime reserve a quarter of the window for output.

## Rejected

- **A separate DeepSeek Core module.** Duplicates nearly all of the OpenAI-compatible Core
  for a handful of differences.
- **`if provider == "deepseek"` branches.** Ruled out by the constraints; differences would
  scatter through the code.
- **Sending the reasoning field only on tool-call turns** (what DeepSeek's docs suggest).
  Live, requests without tools also failed with thinking on. This was the approved plan
  and was changed after the live checks.
- **Changing `_common.py` so configured prices survive.** Left alone on request; merging
  happens inside `openai_compat.py` instead.
- **An explicit maintenance flag on requests.** Needed an `api.py` change; recorded as M14
  and later built (`maintenance-marker.md`).

## Open questions

- M13: one price per model, but DeepSeek's off-peak price is half the peak price, so costs
  are overstated by up to 2x and cost budgets stop early.
- M14: resolved by `InferenceRequest.maintenance` (`maintenance-marker.md`).
- M15: empty compaction output is committed as a summary. Thinking off avoids it for
  DeepSeek only, and setting `thinking` in `options` re-exposes it.
- Prices are dated 2026-10-30 and nothing refreshes them.
- `insufficient_system_resource`, `aborted` and `tool_choice` with thinking were not seen
  live.
