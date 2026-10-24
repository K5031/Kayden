# Continuity memory

**Status:** Implemented.

## Purpose

Continuity gives Kayden one ongoing, person-like conversation without placing
the entire lifetime transcript into every model request. It separates active
context from durable knowledge and searchable historical experience.

Compaction determines what Kayden actively remembers; historical summaries make
older experience recoverable without retaining every raw message forever.

## Stored state

Continuity uses one SQLite database containing four forms of state:

| State | Lifetime | Use |
| --- | --- | --- |
| Active raw messages | Until successfully consolidated | Verbatim recent context and UI restoration |
| Rolling conversation summary | Replaced at each compaction | Bounded continuity for the next turn |
| Long-term memory document | Replaced or explicitly edited | Stable facts, preferences, decisions, and projects |
| Historical summaries | Append-only | Searchable accounts of non-overlapping compacted segments |

The rolling summary and long-term memory are injected automatically. Historical
summaries are not injected; the model retrieves them with `search_history` when
older detail is relevant.

## Turn and compaction flow

For an ordinary turn, the runtime reads the current memory view, applies the
context policy, injects the long-term memory into the system prompt, appends the
new user message, runs the turn, and appends the assistant response.

The context module decides when and how much to compact. A compaction plan keeps
the newest raw turns verbatim and selects an older contiguous segment. One model
call receives:

- the previous rolling summary;
- the previous long-term memory document;
- only the newly selected raw messages.

It must return exactly three outputs:

1. A replacement rolling summary derived from previous summary plus new segment.
2. A replacement long-term memory document.
3. A standalone historical summary of the new segment only.

The runtime parses the structured response, validates all configured token
budgets, and retries once if parsing or size validation fails. A successful
commit atomically replaces the two active documents, appends and indexes the
historical summary, advances the checkpoint, and deletes the covered raw
messages. If validation or commit fails, the previous state and raw messages
remain available.

```text
previous rolling summary --+
previous long-term memory --+--> one consolidation call
new raw segment ------------+          |
                                        +--> replace rolling summary
                                        +--> replace long-term memory
                                        +--> append historical summary
                                                     |
                                              delete covered raw
```

## Gradual decay

The rolling summary has a fixed budget and is repeatedly replaced. Older details
therefore compete with newer context at every consolidation. Relevant old
information may survive indefinitely; repetition, stale details, and noise tend
to disappear. This is model-directed gradual decay, not a deterministic decay
curve or time-based deletion rule.

Durable facts are protected from conversational decay by the separate long-term
memory document. Historical summaries preserve compressed accounts outside the
active context.

## Budgets and retrieval

The cloud context policy defaults to a 60,000-token context with approximately:

- 4,000 tokens for the rolling summary;
- 4,000 tokens for long-term memory;
- 33,000 tokens of recent verbatim conversation;
- 19,000 tokens targeted free for the next turn, tools, and response.

Compaction becomes necessary when fewer than 10,000 tokens are estimated free.
Each historical summary has its own 4,000-token maximum, but the archive has no
aggregate size limit. Local context uses smaller budgets appropriate to its
configured 15,600-token window.

Historical search uses SQLite FTS5 with Porter stemming. It returns three matches
by default, accepts at most twenty, ranks lexically with BM25, and caps combined
returned text at 28,000 characters. This is hybrid storage, not hybrid search:
there is no embedding or semantic retrieval yet.

## Explicit memory actions

Continuity exposes runtime-borrowed tools:

- `remember` appends a concise line immediately;
- `forget_memory` removes one exact normalized line;
- `list_memories` reads the active long-term document;
- `search_history` searches permanent historical summaries.

Automatic consolidation remains the primary mechanism for discovering durable
information. Explicit tools handle immediate user intent and retrieval. Forgetting
ordinary long-term memory currently defaults to allowed; it does not require a
confirmation prompt.

## Invariants

- A historical summary describes only its newly compacted message range.
- Historical ranges do not overlap and are recorded with message checkpoints.
- The cumulative rolling summary is never appended to the historical archive.
- Covered raw messages are deleted only in the successful state transaction.
- Failed or oversized model output cannot replace stored memory.
- The rolling summary, long-term document, and every generated historical entry
  respect the active context policy's individual token budgets.
- Secrets are not intentionally consolidated unless the user explicitly asks to
  remember that exact information.
- Legacy key/value memory migrates once into natural-text memory.

## Trade-offs and limits

Consolidation is lossy and model-dependent. The archive preserves compressed
history, not exact quotations from deleted raw messages. FTS retrieval works well
for remembered terms but can miss semantically related wording; semantic or
hybrid retrieval is a future option once archive size and observed failures
justify its cost.

The database is durable but not currently encrypted. A future retention design
should include export, explicit archive deletion, and private/non-retained turns
without weakening the active-context model.
