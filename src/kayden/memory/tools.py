"""recall, read_note, save_note and forget_note: the tools a Memory module offers the model
(Memory.list_tools, Memory.execute), built only on the Memory contract, so any module can use
them as they are or offer its own.

Notes are found by title, case ignored. Every request already shows the profile and each
other note's title and description; read_note fetches a note's text.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

from kayden.api import (
    PROFILE,
    ArchiveHit,
    Memory,
    MemoryNote,
    Message,
    OperationResult,
    ServerToolUse,
    Text,
    ToolCall,
    ToolContext,
    ToolOutput,
    ToolResult,
    ToolSpec,
)

HIT_PREVIEW_CHARS = 1000
_TITLE = {"type": "string", "minLength": 1}

RECALL = ToolSpec(
    "recall",
    "Search the archive of this conversation, including messages no longer in context. "
    'With scope "all", search the user\'s other conversations too.',
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
    "the user asks you to remember something, or tells you something lasting; Kayden also "
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
    """recall, plus the note tools when the module keeps notes."""
    return (RECALL, READ_NOTE, SAVE_NOTE, FORGET_NOTE) if editable else (RECALL,)


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


def _format_hits(hits: Sequence[ArchiveHit]) -> str:
    if not hits:
        return "No matches."
    lines = []
    for hit in hits:
        m = hit.message
        when = datetime.fromtimestamp(m.created_at, UTC).isoformat(timespec="seconds")
        lines.append(f"[{hit.conversation_id}/{m.id} {m.role} {when}] "
                     f"{_render(m)[:HIT_PREVIEW_CHARS]}")
    return "\n".join(lines)


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
    return " ".join(parts)
