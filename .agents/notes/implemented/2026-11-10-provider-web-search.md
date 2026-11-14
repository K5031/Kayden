# Agent Note: The provider's own web search

Status: implemented — a Core may offer tools its provider runs; with Claude, Anthropic's web search replaces Kadence's free search

## Problem

Kadence's own `web_search` goes through `ddgs`, which reads search engines' public pages
without an API. The engines turn some of those requests away, and `ddgs` then reports "No
results found": common queries failed about one time in three (2026-11-10). Retrying lowers
the misses but cannot remove them, and engines may block more over time. Anthropic runs web
search on its side for API customers: reliable, with citations, billed to the same key at
about $10 per 1 000 searches.

## Decision

### A Core may offer tools its provider runs

- `Core.list_tools()`, optional (default none): tools the provider runs during the
  request itself. The runtime offers them after its built-ins and Memory's and before the
  Tools modules', so Kadence's own `web_search` (a Tools module's) is dropped while the
  provider's is offered, and comes back when it is not. The runtime never runs, gates or
  counts them: they touch nothing on the user's computer. The Core turns each into the
  provider's own tool by name.
- Each use comes back in the Completed message as a `ServerToolUse` block: the provider's
  id, the tool name and input, a one-line `summary` the Core writes ("Searched the web for
  …"), a readable `content` (the pages found), `is_error`, and opaque `state` the Core
  replays to its own provider. Storage, search and other providers use the summary and
  content; other Cores send them as text, so a model switch keeps what was found.
- `Usage.extra_cost_usd` carries what a provider charges beyond tokens; the budget adds
  it to the cost.
- Frontends show a `ServerToolUse` as a finished step (`frontends.common.provider_step`).

### The Anthropic Core

- Offers `web_search`: `web_search_20260209` (dynamic filtering) where the model has it,
  else `web_search_20250305`, at most 5 searches per request, $0.01 each.
- Keeps every server-side block of a turn as the API sent it. With dynamic filtering the
  model runs code that calls the search, and the blocks refer to each other by id, so they
  are kept together, in their order, on the turn's first shown use, and sent back
  unchanged. The filtering code is not shown as a step.
- Sends them back only within the turn that made them (after the user's latest message):
  the results hold the pages' text, which every later request would pay for again. Earlier
  turns' searches go as text; the model searches again when it needs more.
- Continues a turn the API paused (`pause_turn`) within the same request, up to 5 times.
- Lists the sources cited in the answer as a final "Sources:" line of links.
- Falls back on a 400: web search switched off for the account turns it off for the
  instance; server blocks the API will not take back are sent again as text.

Choices are listed in `.agents/decisions.md` (The provider's web search). Kadence's own
search stays for other providers: [web search](2026-11-10-web-search.md).

## Alternatives considered

- **Retries only** (shipped first). Fewer misses, same cause.
- **A search API with its own key** (Brave, Tavily, Exa). Reliable, but another account,
  and paid beyond a small free tier; the user declined it.
- **The provider's search as a Tools module.** A Tools module cannot see inside the
  provider's request; the search runs there, mid-answer.
- **Sending searches back in every later request.** Best recall of what was read, but in a
  test with Opus 5.5 the second turn cost about three times the first.
- **One block per use and result,** replayed pair by pair. Simpler, but dynamic filtering's
  blocks refer to each other across pairs, and the API refused the reordered set.

## Consequences

- Search with Claude is reliable and cites its sources; nothing changes with DeepSeek or
  OpenAI.
- Cost: about 1 cent per search plus its results as input tokens; on Opus a searched answer
  costs roughly 10 to 15 cents (2026-11-10). The spending limit covers both.
- Later turns know which pages were found but not their text; the model searches or reads
  a page again when it needs more.
- The message format has one more block type, which every Core, Memory and frontend
  handles.
