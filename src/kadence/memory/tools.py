"""recall, read_around, read_note, save_note and forget_note: the tools a Memory module
offers the model
(Memory.list_tools, Memory.execute), built only on the Memory contract, so any module can use
them as they are or offer its own.

Notes are found by title, case ignored. Every request already shows the profile and each
other note's title and description; read_note fetches a note's text.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

from kadence.api import (
    PROFILE,
    ArchiveHit,
    Document,
    File,
    Image,
    Memory,
    MemoryNote,
    Message,
    OperationResult,
    Role,
    ServerToolUse,
    Text,
    ToolCall,
    ToolContext,
    ToolOutput,
    ToolResult,
    ToolSpec,
    attachment_text,
)

HIT_PREVIEW_CHARS = 1000
AROUND_MAX = 20  # read_around: messages on either side at most
_TITLE = {"type": "string", "minLength": 1}
_SIDE = {"type": "integer", "minimum": 0, "maximum": AROUND_MAX, "default": 3}

RECALL = ToolSpec(
    "recall",
    "Search the archive of this conversation, including messages no longer in context. "
    'With scope "all", search the user\'s other conversations too. Each hit is one message; '
    "use read_around to see it with the messages around it.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "default": 5},
            "scope": {"type": "string", "enum": ["conversation", "all"],
                      "default": "conversation"},
        },
        "required": ["query"],
    },
    read_only=True,
)
READ_AROUND = ToolSpec(
    "read_around",
    "Read a message from the archive with the messages before and after it, in order: "
    "what a recall hit was answering, or what came of it. Give the hit's message id, and "
    "its conversation id when it is from another conversation.",
    {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "minLength": 1},
            "conversation_id": {"type": "string",
                                "description": "Default: this conversation."},
            "before": _SIDE,
            "after": _SIDE,
        },
        "required": ["message_id"],
    },
    read_only=True,
)
READ_NOTE = ToolSpec(
    "read_note",
    "Read one of your memory notes, listed under Memory notes, when it is relevant to what "
    "the user asks.",
    {"type": "object", "properties": {"title": _TITLE}, "required": ["title"]},
    read_only=True,
)
SAVE_NOTE = ToolSpec(
    "save_note",
    f"Write a memory note that every later conversation can use: \"{PROFILE}\" for who the "
    "user is and how they want answers, or one note per project, person or topic. A note "
    "with this title is replaced, so read it first and write its whole new text. Save when "
    "the user asks you to remember something, or tells you something lasting; Kadence also "
    "saves on its own after each answer. Never save passwords, keys or other secrets.",
    {"type": "object", "properties": {
        "title": _TITLE,
        "description": {"type": "string", "description": "One line: what the note is about."},
        "text": {"type": "string"}},
     "required": ["title", "description", "text"]},
)
FORGET_NOTE = ToolSpec(
    "forget_note",
    "Delete a memory note, when the user asks or it is no longer true. To drop one fact, "
    "save the note without it instead.",
    {"type": "object", "properties": {"title": _TITLE}, "required": ["title"]},
)


def memory_tools(*, editable: bool) -> tuple[ToolSpec, ...]:
    """recall and read_around, plus the note tools when the module keeps notes."""
    return ((RECALL, READ_AROUND, READ_NOTE, SAVE_NOTE, FORGET_NOTE) if editable
            else (RECALL, READ_AROUND))


async def run_memory_tool(memory: Memory, call: ToolCall, ctx: ToolContext) -> ToolOutput:
    args = call.arguments
    match call.name:
        case "recall":
            scope = [ctx.conversation_id]
            if args.get("scope", "conversation") == "all":
                scope += [c.id for c in await memory.list_conversations()
                          if c.id != ctx.conversation_id]
            hits = await memory.search(args["query"], conversation_ids=scope,
                                       limit=args.get("limit", 5))
            return ToolOutput(_format_hits(hits))
        case "read_around":
            return await _read_around(memory, args.get("conversation_id") or ctx.conversation_id,
                                      args["message_id"], args.get("before", 3),
                                      args.get("after", 3))
        case "read_note":
            note = await _titled(memory, args["title"])
            if note is None:
                return await _missing(memory, args["title"])
            return ToolOutput(f"# {note.title}\n{note.description}\n\n{note.text or '(empty)'}")
        case "save_note":
            try:
                note = await memory.save_note(args["title"], args["description"], args["text"],
                                              conversation_id=ctx.conversation_id)
            except ValueError as e:
                return ToolOutput(str(e), is_error=True)
            return ToolOutput(f"Saved {note.title!r}.")
        case "forget_note":
            note = await _titled(memory, args["title"])
            if note is None:
                return await _missing(memory, args["title"])
            try:
                await memory.delete_note(note.id)
            except ValueError as e:
                return ToolOutput(str(e), is_error=True)
            return ToolOutput(f"Forgotten: {note.title}")
    return ToolOutput(f"Unknown tool {call.name!r}.", is_error=True)


async def _titled(memory: Memory, title: str) -> MemoryNote | None:
    key = " ".join(title.split()).casefold()
    return next((n for n in await memory.notes() if n.title.casefold() == key), None)


async def _missing(memory: Memory, title: str) -> ToolOutput:
    listed = "\n".join(f"- {n.title}" for n in await memory.notes())
    return ToolOutput(f"No note is titled {title!r}. Notes:\n{listed}", is_error=True)


async def _read_around(memory: Memory, conversation_id: str, message_id: str, before: int,
                       after: int) -> ToolOutput:
    try:
        # Read on from the message, then back from the first one after it: the message
        # itself is the last of those, and the history needs no lookup by id.
        later = await memory.history(conversation_id, after=message_id, limit=max(after, 1))
        upto = (await memory.history(conversation_id, before=later[0].id, limit=before + 1)
                if later else await memory.history(conversation_id, limit=before + 1))
    except KeyError:
        return ToolOutput(f"No message {message_id} in conversation {conversation_id}.",
                          is_error=True)
    if not upto or upto[-1].id != message_id:
        return ToolOutput(f"No message {message_id} in conversation {conversation_id}.",
                          is_error=True)
    return ToolOutput("\n".join(_line(conversation_id, m, mark=m.id == message_id)
                                for m in [*upto, *later[:after]]))


def _format_hits(hits: Sequence[ArchiveHit]) -> str:
    if not hits:
        return "No matches."
    return "\n".join(_line(hit.conversation_id, hit.message) for hit in hits)


def _line(conversation_id: str, m: Message, *, mark: bool = False) -> str:
    when = datetime.fromtimestamp(m.created_at, UTC).isoformat(timespec="seconds")
    who = m.role if m.role is not Role.USER or m.from_user else "notice"  # Kadence's own
    return (f"{'→ ' if mark else ''}[{conversation_id}/{m.id} {who} {when}] "
            f"{_render(m)[:HIT_PREVIEW_CHARS]}")


def _render(message: Message) -> str:
    parts = []
    for b in message.blocks:
        match b:
            case Text(text=t):
                parts.append(t)
            case ToolCall(name=n, arguments=a):
                parts.append(f"[call {n} {json.dumps(a)}]")
            case ToolResult(status=s, content=c) | OperationResult(status=s, content=c):
                parts.append(f"[{s}] {c}")
            case ServerToolUse(summary=summary, content=c):
                parts.append(f"[{summary}] {c}")
            case Image() | File() | Document():
                parts.append(attachment_text(b))
    return " ".join(parts)
