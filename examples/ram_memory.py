"""A minimal Memory: everything in RAM, no summaries, gone when Kadence quits.

It shows the eight methods a Memory must have. A real one also keeps the archive on disk
and summarizes a long conversation in maintain(); the easiest way there is to build on
kadence.memory.sqlite.SqliteArchive, as kadence.memory.files does.

Use it with `[memory] factory = "examples.ram_memory:factory"` in config.toml.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Collection, Sequence
from dataclasses import replace
from typing import Any

from kadence.api import (
    ArchiveHit,
    AssembledContext,
    Completion,
    ContextRequest,
    ConversationId,
    ConversationInfo,
    InstancePaths,
    Memory,
    Message,
    Role,
    Text,
)


class RamMemory(Memory):
    def __init__(self) -> None:
        self._infos: dict[ConversationId, ConversationInfo] = {}
        self._messages: dict[ConversationId, list[Message]] = {}

    async def create_conversation(self, *, title: str | None = None,
                                  seed_from: ConversationId | None = None,
                                  folder: str | None = None,
                                  conversation_id: ConversationId | None = None
                                  ) -> ConversationInfo:
        if seed_from is not None and seed_from not in self._infos:
            raise KeyError(seed_from)
        now = time.time()
        info = ConversationInfo(conversation_id or uuid.uuid4().hex, title, now, now,
                                seed_from=seed_from, folder=folder)
        self._infos[info.id], self._messages[info.id] = info, []
        return info

    async def list_conversations(self, *, include_archived: bool = False
                                 ) -> Sequence[ConversationInfo]:
        shown = [i for i in self._infos.values() if include_archived or not i.archived]
        return sorted(shown, key=lambda i: i.updated_at, reverse=True)

    async def update_conversation(self, conversation_id: ConversationId, *,
                                  title: str | None = None, archived: bool | None = None
                                  ) -> ConversationInfo:
        info = self._infos[conversation_id]  # KeyError for an unknown id, as the contract says
        if title is not None:
            info = replace(info, title=title.strip() or None)
        if archived is not None:
            info = replace(info, archived=archived)
        self._infos[conversation_id] = info
        return info

    async def append(self, conversation_id: ConversationId, messages: Sequence[Message]
                     ) -> None:
        stored = self._messages[conversation_id]
        known = {m.id: m for m in stored}
        if any(m.id in known and known[m.id] != m for m in messages):
            raise ValueError("a message id is already used for another message")
        stored.extend(m for m in messages if m.id not in known)  # the same again: a no-op
        self._infos[conversation_id] = replace(self._infos[conversation_id],
                                               updated_at=time.time())

    async def history(self, conversation_id: ConversationId, *, before: str | None = None,
                      limit: int | None = None, after: str | None = None
                      ) -> Sequence[Message]:
        messages = self._messages[conversation_id]
        ids = [m.id for m in messages]
        if before is not None:
            messages = messages[:ids.index(before)] if before in ids else _unknown(before)
        if after is not None:
            if after not in ids:
                _unknown(after)
            messages = [m for m in messages if ids.index(m.id) > ids.index(after)]
        if limit is not None:
            messages = messages[:limit] if after is not None else messages[-limit:]
        return list(messages)

    async def assemble(self, request: ContextRequest) -> AssembledContext:
        """What the model sees: here the newest messages that fit, starting at a user
        message so that no tool call is cut from its result. No summary, no long-term
        memory."""
        messages = self._messages[request.conversation_id]
        starts = [i for i, m in enumerate(messages) if m.role is Role.USER]
        for start in starts:
            if request.count_tokens(messages[start:]) <= request.max_tokens:
                return AssembledContext("", messages[start:])
        return AssembledContext("", messages[-1:])

    async def maintain(self, conversation_id: ConversationId, complete: Completion,
                       context: ContextRequest) -> bool:
        """Where a real Memory summarizes older messages (with `complete`, a model call)
        and saves what it learned. This one does nothing."""
        self._messages[conversation_id]  # KeyError for an unknown conversation
        return False

    async def search(self, query: str, *,
                     conversation_ids: Collection[ConversationId] | None = None,
                     limit: int = 5) -> Sequence[ArchiveHit]:
        hits = [ArchiveHit(cid, message)
                for cid, messages in self._messages.items()
                if conversation_ids is None or cid in conversation_ids
                for message in messages
                if any(isinstance(b, Text) and query.lower() in b.text.lower()
                       for b in message.blocks)]
        return hits[-limit:][::-1]  # newest first


def _unknown(message_id: str) -> Any:
    raise KeyError(message_id)


def factory(options: dict[str, Any], paths: InstancePaths) -> RamMemory:
    return RamMemory()


factory.display_name = "In memory only"  # type: ignore[attr-defined]
