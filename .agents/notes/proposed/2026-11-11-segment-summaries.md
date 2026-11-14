# Agent Note: Segment summaries

Status: proposed — keep a summary per stretch of conversation, searchable, each tied to its messages

## Problem

`recall` finds messages by their words. A vague question ("what did we decide about the
sidebar?") often shares few words with the messages that answer it, and a keyword hit is
one message on its own. `read_around` shows a hit with its neighbours, but there is no
overview of a long conversation's stretches to find the right one by meaning first.

SqliteMemory keeps one summary per conversation and replaces it at each compaction: the
new one rewrites the old summary plus the next stretch. Every earlier summary is lost,
and none says which messages it covers on its own.

## Proposal

- **One summary per stretch.** Each compaction also writes a summary of only the messages
  it compacts: from the previous summary's `through_seq` to its own. The rolling summary
  in the context stays as it is; segment summaries are kept alongside it, never replaced.
- **Searchable.** Segment summaries are indexed like messages (FTS5). `recall` returns
  matching summaries with their message range, ahead of or next to message hits.
- **Tied to their messages.** A summary hit names its first and last message, so the
  agent opens the stretch with `read_around`, or a variant that reads a range.
- Storage: a new table with the conversation, the seq range, the text and when it was
  written; a new schema version. Summaries already compacted are not rebuilt.

## Alternatives considered

- **Keep every rolling summary.** No new prompt, but each one repeats and compresses the
  ones before it: a chain of ever-shorter versions of the same history, not a summary of
  each stretch, and none is tied to a range of messages.
- **Return neighbours with every recall hit.** No new storage, but it spends tokens on
  context for hits the agent does not need; `read_around` opens it on demand instead.
- **Embeddings for semantic search.** Finds meaning without a summary per stretch, but
  needs an embedding model or service, a dependency Kadence does not have.

## Acceptance criteria

- A compaction writes one segment summary covering exactly the messages it compacted.
- `recall` finds a segment summary by words in it that no message in its range contains,
  and the hit names the range.
- The agent can open a summary's messages in one call.
- Deleting a conversation deletes its segment summaries.

## Risks

- One more model output per compaction: more tokens, or a combined prompt that must
  produce both the rolling summary and the segment summary reliably.
- A summary hit that reads well but misleads: the raw messages stay the source of truth,
  so the agent should open them before relying on a detail.
- Build only after real use shows `recall` and `read_around` falling short; until then
  the cost is not known to buy anything.
