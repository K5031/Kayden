"""In-memory Memory implementing the full contract. The MemoryStore outlives runtime
instances, so a new InMemoryMemory over the same store models a restart."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from kayden.api import (
    ArchiveHit,
    AssembledContext,
    Block,
    Completion,
    ContextRequest,
    ConversationId,
    ConversationInfo,
    CoreError,
    Memory,
    MemoryEntry,
    MemoryView,
    Message,
    OperationResult,
    Role,
    Text,
    ToolCall,
    ToolResult,
)


@dataclass
class Conversation:
    info: ConversationInfo
    messages: list[Message] = field(default_factory=list)
    summary: str = ""
    active_from: int = 0
    compactions: int = 0


@dataclass
class MemoryStore:
    conversations: dict[ConversationId, Conversation] = field(default_factory=dict)
    # entry id -> revisions, oldest first; the last one is current
    long_term: dict[str, list[MemoryEntry]] = field(default_factory=dict)
    forgotten: set[str] = field(default_factory=set)
    next_id: int = 0

    def add(self, text: str, source: ConversationId | None) -> MemoryEntry:
        self.next_id += 1
        entry = MemoryEntry(str(self.next_id), text, source, time.time())
        self.long_term[entry.id] = [entry]
        return entry

    def entries(self) -> list[MemoryEntry]:
        return [revisions[-1] for revisions in self.long_term.values()]


def render_block(block: Block) -> str:
    match block:
        case Text(text=t):
            return t
        case ToolCall(name=n, arguments=a):
            return f"[call {n} {json.dumps(a)}]"
        case ToolResult(status=s, content=c) | OperationResult(status=s, content=c):
            return f"[{s}] {c}"
    return ""


def render(message: Message) -> str:
    return " ".join(render_block(b) for b in message.blocks)


class InMemoryMemory(Memory):
    def __init__(
        self,
        store: MemoryStore | None = None,
        *,
        compact_at: float = 0.75,
        cut: float = 0.5,
        summary_tokens: int = 200,
        maintain_delay: float = 0.0,
        trace: list[Any] | None = None,
        auto_title: bool = False,
        editable: bool = True,
    ):
        self.store = store if store is not None else MemoryStore()
        self.compact_at = compact_at
        self.cut = cut
        self.summary_tokens = summary_tokens
        self.trace = trace if trace is not None else []
        self.fail_append = 0  # number of upcoming appends that raise
        self.maintain_error: Exception | None = None
        self.maintain_delay = maintain_delay
        self.maintain_calls = 0
        self.search_error: Exception | None = None
        self.mutating = 0
        self.max_mutating = 0
        self.assemble_budgets: list[int] = []
        self.seq_source: Callable[[], int] | None = None  # runtime seq at append time
        self.maintain_override: Callable[[Completion], Awaitable[bool]] | None = None
        self.auto_title = auto_title  # write titles during maintain with a light call
        self.editable = editable  # False: the optional memory methods are unsupported
        self.extract: list[str] = []  # facts the next compaction extracts

    def _conv(self, conversation_id: ConversationId) -> Conversation:
        return self.store.conversations[conversation_id]  # KeyError for unknown ids

    async def create_conversation(self, *, title: str | None = None,
                                  seed_from: ConversationId | None = None,
                                  folder: str | None = None) -> ConversationInfo:
        if seed_from is not None:
            self._conv(seed_from)
        now = time.time()
        info = ConversationInfo(uuid.uuid4().hex, title, now, now, seed_from=seed_from,
                                folder=folder)
        self.store.conversations[info.id] = Conversation(info)
        return info

    async def list_conversations(self, *, include_archived: bool = False
                                 ) -> Sequence[ConversationInfo]:
        infos = [self._info(c) for c in self.store.conversations.values()
                 if include_archived or not c.info.archived]
        return sorted(infos, key=lambda i: i.updated_at, reverse=True)

    def _info(self, conv: Conversation) -> ConversationInfo:
        first = next((m for m in conv.messages if m.role == Role.USER), None)
        return replace(conv.info, preview=render(first)[:100] if first else None)

    async def update_conversation(self, conversation_id: ConversationId, *,
                                  title: str | None = None, archived: bool | None = None
                                  ) -> ConversationInfo:
        conv = self._conv(conversation_id)
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = title.strip() or None
        if archived is not None:
            changes["archived"] = archived
        conv.info = replace(conv.info, **changes)
        return self._info(conv)

    async def delete_conversation(self, conversation_id: ConversationId) -> None:
        if not self.editable:
            raise NotImplementedError
        self._conv(conversation_id)
        del self.store.conversations[conversation_id]
        for entry_id, revisions in self.store.long_term.items():
            if revisions[0].source_conversation_id == conversation_id:
                self.store.long_term[entry_id] = [
                    replace(r, source_conversation_id=None) for r in revisions]
        for conv in self.store.conversations.values():
            if conv.info.seed_from == conversation_id:
                conv.info = replace(conv.info, seed_from=None)

    async def memories(self) -> Sequence[MemoryEntry]:
        if not self.editable:
            raise NotImplementedError
        return self.store.entries()

    async def remember(self, text: str, *,
                       conversation_id: ConversationId | None = None) -> MemoryEntry:
        if not self.editable:
            raise NotImplementedError
        return self.store.add(text, conversation_id)

    async def revise(self, entry_id: str, text: str) -> MemoryEntry:
        if not self.editable:
            raise NotImplementedError
        revisions = self.store.long_term[entry_id]
        revisions.append(replace(revisions[-1], text=text))
        return revisions[-1]

    async def forget(self, entry_id: str) -> None:
        if not self.editable:
            raise NotImplementedError
        for entry in self.store.long_term.pop(entry_id):
            self.store.forgotten.add(entry.text)

    async def append(self, conversation_id: ConversationId, messages: Sequence[Message]) -> None:
        conv = self._conv(conversation_id)
        self.mutating += 1
        self.max_mutating = max(self.max_mutating, self.mutating)
        try:
            await asyncio.sleep(0)
            if self.fail_append:
                self.fail_append -= 1
                raise OSError("disk full")
            existing = {m.id: m for m in conv.messages}
            for m in messages:
                if m.id in existing:
                    if existing[m.id] != m:
                        raise ValueError(f"message {m.id} re-appended with different content")
                    continue
                conv.messages.append(m)
                existing[m.id] = m
                seq = self.seq_source() if self.seq_source else None
                self.trace.append(("append", m.role, m.id, seq))
            conv.info = replace(conv.info, updated_at=time.time())
        finally:
            self.mutating -= 1

    async def history(self, conversation_id: ConversationId, *, before: str | None = None,
                      limit: int | None = None) -> Sequence[Message]:
        msgs = list(self._conv(conversation_id).messages)
        if before is not None:
            msgs = msgs[: next(i for i, m in enumerate(msgs) if m.id == before)]
        if limit is not None:
            msgs = msgs[-limit:] if limit else []
        return msgs

    def _addendum(self, conv: Conversation) -> str:
        parts = []
        if conv.summary:
            parts.append(f"Summary of earlier conversation:\n{conv.summary}")
        if lt := [e.text for e in self.store.entries()]:
            parts.append("Long-term memory:\n" + "\n".join(lt))
        return "\n\n".join(parts)

    async def assemble(self, request: ContextRequest) -> AssembledContext:
        self.trace.append(("assemble", request.max_tokens))
        self.assemble_budgets.append(request.max_tokens)
        conv = self._conv(request.conversation_id)
        count = request.count_tokens
        addendum = self._addendum(conv)
        while addendum and count(addendum) > request.max_tokens // 2:
            addendum = addendum[: len(addendum) // 2]
        budget = request.max_tokens - (count(addendum) if addendum else 0)
        active = conv.messages[conv.active_from :]
        starts = [i for i, m in enumerate(active) if m.role == Role.USER]
        if not starts:
            return AssembledContext(addendum, [])
        chosen = next((i for i in starts if count(active[i:]) <= budget), starts[-1])
        view = list(active[chosen:])
        while count(view) > budget:
            j = max(range(len(view)), key=lambda k: count([view[k]]))
            m = view[j]
            text = render(m)
            if len(text) < 16:
                break
            marker = f" [truncated; recall message {m.id} for the full text]"
            view[j] = replace(m, blocks=(Text(text[: len(text) // 2] + marker),))
        return AssembledContext(addendum, view)

    async def maintain(self, conversation_id: ConversationId, complete: Completion,
                       context: ContextRequest) -> bool:
        conv = self._conv(conversation_id)
        self.maintain_calls += 1
        self.trace.append(("maintain_start",))
        self.mutating += 1
        self.max_mutating = max(self.max_mutating, self.mutating)
        try:
            if self.maintain_delay:
                await asyncio.sleep(self.maintain_delay)
            if self.maintain_error is not None:
                raise self.maintain_error
            if self.maintain_override is not None:
                return await self.maintain_override(complete)
            acted = self._seed(conv)
            acted = await self._compact(conv, complete, context) or acted
            return await self._title(conv, complete) or acted
        finally:
            self.mutating -= 1
            self.trace.append(("maintain_end",))

    def _seed(self, conv: Conversation) -> bool:
        """The initial summary of a seeded conversation: the source's summary and tail."""
        source = self.store.conversations.get(conv.info.seed_from or "")
        if source is None or conv.compactions or conv.summary:
            return False
        tail = "\n".join(render(m) for m in source.messages[source.active_from :])
        conv.summary = "\n".join(p for p in (source.summary, tail) if p)
        return bool(conv.summary)

    async def _title(self, conv: Conversation, complete: Completion) -> bool:
        first = next((m for m in conv.messages if m.role == Role.USER), None)
        if not self.auto_title or conv.info.title or first is None:
            return False
        try:
            title = (await complete("Write a short title.", [first], max_tokens=20,
                                    light=True)).strip()
        except CoreError:
            return False  # retried by a later maintain
        if not title or conv.info.title:
            return False
        conv.info = replace(conv.info, title=title)
        return True

    async def _compact(self, conv: Conversation, complete: Completion,
                       context: ContextRequest) -> bool:
        active = conv.messages[conv.active_from :]
        if context.count_tokens(active) <= self.compact_at * context.max_tokens:
            return False
        target = len(active) * self.cut
        cuts = [i for i, m in enumerate(active) if m.role == Role.USER and i > 0]
        if not cuts:
            return False
        j = next((i for i in cuts if i >= target), cuts[-1])
        selected = "\n".join(render(m) for m in active[:j])
        prompt = Message(
            uuid.uuid4().hex,
            Role.USER,
            (Text(f"Previous summary:\n{conv.summary}\n\nNew messages:\n{selected}"),),
            time.time(),
        )
        summary = await complete(
            "Summarize the conversation.", [prompt], max_tokens=self.summary_tokens,
            light=True,
        )
        # Atomic: summary and compaction point change together, after inference.
        conv.summary, conv.active_from = summary, conv.active_from + j
        conv.compactions += 1
        known = {e.text for e in self.store.entries()} | self.store.forgotten
        for fact in self.extract:
            if fact not in known:
                self.store.add(fact, conv.info.id)
        return True

    async def search(self, query: str, *,
                     conversation_ids: Collection[ConversationId] | None = None,
                     limit: int = 5) -> Sequence[ArchiveHit]:
        if self.search_error is not None:
            raise self.search_error
        words = set(re.findall(r"\w+", query.lower()))
        hits = []
        for cid in conversation_ids or ():
            self._conv(cid)
        for conv in self.store.conversations.values():
            if conversation_ids is not None and conv.info.id not in conversation_ids:
                continue
            for m in conv.messages:
                score = len(words & set(re.findall(r"\w+", render(m).lower())))
                if score:
                    hits.append(ArchiveHit(conv.info.id, m, float(score)))
        hits.sort(key=lambda h: -(h.score or 0))
        return hits[:limit]

    async def inspect(self, conversation_id: ConversationId) -> MemoryView | None:
        conv = self._conv(conversation_id)
        long_term = [e.text for e in self.store.entries()]
        return MemoryView(conv.summary, long_term)


def factory(options: dict[str, Any], paths: Any) -> InMemoryMemory:
    return InMemoryMemory(options.get("store"))
