"""recall, remember and forget: the tools a Memory module offers the model (Memory.list_tools,
Memory.execute), built only on the Memory contract, so any module can use them as they are
or offer its own.

Facts compare with case and whitespace ignored: remember keeps no second copy of a fact,
and forget erases every entry whose text matches.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

from kayden.api import (
    ArchiveHit,
    Memory,
    Message,
    OperationResult,
    Text,
    ToolCall,
    ToolContext,
    ToolOutput,
    ToolResult,
    ToolSpec,
)

HIT_PREVIEW_CHARS = 1000

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
REMEMBER = ToolSpec(
    "remember",
    "Keep a fact in long-term memory, which every conversation with the user sees. Use it "
    "when the user asks you to remember something, or tells you something lasting about "
    "themselves, their work or their preferences that will matter later. One short fact "
    "per call, written about the user (\"Is vegetarian\"). Never keep passwords, keys or "
    "other secrets. Say in your reply what you remembered.",
    {"type": "object", "properties": {"text": {"type": "string", "minLength": 1}},
     "required": ["text"]},
)
FORGET = ToolSpec(
    "forget",
    "Remove a fact from long-term memory, when the user asks or it is no longer true. "
    "`text` is the fact as it appears in your long-term memory.",
    {"type": "object", "properties": {"text": {"type": "string", "minLength": 1}},
     "required": ["text"]},
)


def memory_tools(*, editable: bool) -> tuple[ToolSpec, ...]:
    """recall, plus remember and forget when the module can edit long-term memory."""
    return (RECALL, REMEMBER, FORGET) if editable else (RECALL,)


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
        case "remember":
            text = " ".join(args["text"].split())
            if not text:
                return ToolOutput("The memory is empty.", is_error=True)
            if any(_same(e.text, text) for e in await memory.memories()):
                return ToolOutput("Already remembered.")
            await memory.remember(text, conversation_id=ctx.conversation_id)
            return ToolOutput(f"Remembered: {text}")
        case "forget":
            entries = await memory.memories()
            matches = [e for e in entries if _same(e.text, args["text"])]
            if not matches:
                listed = "\n".join(f"- {e.text}" for e in entries) or "(none)"
                return ToolOutput(f"No memory says that. Long-term memory:\n{listed}",
                                  is_error=True)
            for entry in matches:
                await memory.forget(entry.id)
            return ToolOutput(f"Forgotten: {matches[0].text}")
    return ToolOutput(f"Unknown tool {call.name!r}.", is_error=True)


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


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
    return " ".join(parts)
