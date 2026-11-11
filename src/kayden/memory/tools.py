"""recall, read_around and memory: the tools a Memory module offers the model
(Memory.list_tools, Memory.execute), built only on the Memory contract, so any module can use
them as they are or offer its own.

memory works on the open conversation's project memory folder (Memory.memory_files) with the
commands of Anthropic's memory tool, which Claude models know.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

from kayden.api import (
    MEMORY_INDEX,
    ArchiveHit,
    Memory,
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
MEMORY = ToolSpec(
    "memory",
    "Read and write your long-term memory for this project: the memory folder whose index, "
    f"{MEMORY_INDEX}, is in your instructions. Paths are relative to the folder. Commands: "
    "view (a file with line numbers, or the folder's files when path is empty or \".\"), "
    "create (a file with file_text, replacing one that exists), str_replace (old_str, which "
    "must occur once, with new_str), insert (insert_text after line insert_line; 0 is the "
    "top), delete, rename (old_path to new_path).",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert",
                                                   "delete", "rename"]},
            "path": {"type": "string"},
            "file_text": {"type": "string"},
            "old_str": {"type": "string"},
            "new_str": {"type": "string"},
            "insert_line": {"type": "integer", "minimum": 0},
            "insert_text": {"type": "string"},
            "old_path": {"type": "string"},
            "new_path": {"type": "string"},
        },
        "required": ["command"],
    },
)


def memory_tools(*, editable: bool) -> tuple[ToolSpec, ...]:
    """recall and read_around, plus memory when the module keeps memory files."""
    return (RECALL, READ_AROUND, MEMORY) if editable else (RECALL, READ_AROUND)


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
        case "memory":
            folder = await _folder_of(memory, ctx.conversation_id)
            try:
                return ToolOutput(await _memory_command(memory, folder, args))
            except (KeyError, ValueError) as e:
                return ToolOutput(str(e.args[0] if e.args else e), is_error=True)
    return ToolOutput(f"Unknown tool {call.name!r}.", is_error=True)


async def _folder_of(memory: Memory, conversation_id: str) -> str | None:
    """The work folder of a conversation, whose project memory the tool works on."""
    for info in await memory.list_conversations(include_archived=True):
        if info.id == conversation_id:
            return info.folder
    return None


async def _memory_command(memory: Memory, folder: str | None, args: dict) -> str:
    """Run one memory command; KeyError or ValueError say what is wrong."""
    command, path = args["command"], args.get("path", "").strip()

    async def read(name: str) -> str:
        for file in await memory.memory_files(folder):
            if file.path == name:
                return file.text
        raise KeyError(f"No memory file {name}. Use view with an empty path to list them.")

    def needs(*keys: str) -> None:
        if missing := [k for k in keys if k not in args]:
            raise ValueError(f"{command} needs {', '.join(missing)}.")

    match command:
        case "view" if path in ("", ".", "/"):
            files = await memory.memory_files(folder)
            return "\n".join(f"{f.path} ({len(f.text)} characters)" for f in files) or (
                "The memory folder is empty.")
        case "view":
            lines = (await read(path)).splitlines()
            return "\n".join(f"{i:6}\t{line}" for i, line in enumerate(lines, 1)) or "(empty)"
        case "create":
            needs("path", "file_text")
            await memory.write_memory_file(folder, path, args["file_text"])
            return f"Saved {path}."
        case "str_replace":
            needs("path", "old_str", "new_str")
            text = await read(path)
            count = text.count(args["old_str"])
            if count != 1:
                raise ValueError(f"old_str occurs {count} times in {path}; it must occur once.")
            await memory.write_memory_file(folder, path,
                                           text.replace(args["old_str"], args["new_str"]))
            return f"Edited {path}."
        case "insert":
            needs("path", "insert_line", "insert_text")
            lines = (await read(path)).splitlines(keepends=True)
            at = args["insert_line"]
            if at > len(lines):
                raise ValueError(f"{path} has {len(lines)} lines.")
            added = args["insert_text"] if args["insert_text"].endswith("\n") else (
                args["insert_text"] + "\n")
            if at and not lines[at - 1].endswith("\n"):
                lines[at - 1] += "\n"
            await memory.write_memory_file(folder, path, "".join([*lines[:at], added,
                                                                    *lines[at:]]))
            return f"Inserted into {path}."
        case "delete":
            needs("path")
            await memory.delete_memory_file(folder, path)
            return f"Deleted {path}."
        case "rename":
            needs("old_path", "new_path")
            text = await read(args["old_path"])
            await memory.write_memory_file(folder, args["new_path"], text)
            await memory.delete_memory_file(folder, args["old_path"])
            return f"Renamed {args['old_path']} to {args['new_path']}."
    raise ValueError(f"Unknown command {command!r}.")


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
    return (f"{'→ ' if mark else ''}[{conversation_id}/{m.id} {m.role} {when}] "
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
    return " ".join(parts)
