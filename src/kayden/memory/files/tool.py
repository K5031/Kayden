"""The memory tool: the agent reads and writes its project's memory folder with the commands
of Anthropic's memory tool, which Claude models know. Blocking: run from a worker thread."""

from __future__ import annotations

from typing import Any

from kayden.api import ToolSpec
from kayden.memory.files.folder import MEMORY_INDEX, MemoryFolder

INDEX_MAX_LINES = 200  # of MEMORY.md: a longer index is condensed before more goes in
MEMORY_MAX_CHARS = 4000  # of one memory file: a memory is one fact

MEMORY = ToolSpec(
    "memory",
    "Read and write your long-term memory for this project: the memory folder whose index, "
    f"{MEMORY_INDEX}, is in your instructions. Paths are relative to the folder. Commands: "
    "view (a file with line numbers, or the folder's files when path is empty or \".\"), "
    "create (a new file with file_text), str_replace (old_str, which must occur once, with "
    "new_str), insert (insert_text after line insert_line; 0 is the top), delete, rename "
    "(old_path to new_path). View a file before you change it: a file you have not read "
    "in this conversation, or that changed since, is not edited, deleted or renamed. A "
    f"memory holds at most {MEMORY_MAX_CHARS} characters and {MEMORY_INDEX} at most "
    f"{INDEX_MAX_LINES} lines.",
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


def run_command(folder: MemoryFolder, args: dict[str, Any], seen: dict[str, str]) -> str:
    """Run one memory command; KeyError or ValueError say what is wrong. `seen` is what the
    conversation has read: each file's text as it last viewed or wrote it. A file is
    changed only when that is still its text, so nothing is edited or removed unread."""
    command, path = args["command"], args.get("path", "").strip()

    def needs(*keys: str) -> None:
        if missing := [k for k in keys if k not in args]:
            raise ValueError(f"{command} needs {', '.join(missing)}.")

    def read(name: str) -> str:
        try:
            return folder.read(name).text
        except KeyError:
            raise KeyError(f"No memory file {name}. Use view with an empty path to list them."
                           ) from None

    def known(name: str) -> str:
        """The file's text, which the conversation must have read as it is now."""
        text = read(name)
        if name not in seen:
            raise ValueError(f"View {name} first: you have not read it in this conversation.")
        if seen[name] != text:
            del seen[name]
            raise ValueError(f"View {name} first: it changed since you read it.")
        return text

    def write(name: str, text: str) -> None:
        if name == MEMORY_INDEX and len(text.splitlines()) > INDEX_MAX_LINES:
            raise ValueError(
                f"{MEMORY_INDEX} would have {len(text.splitlines())} lines; it holds at most "
                f"{INDEX_MAX_LINES}. Condense it first: merge memories that belong together, "
                "delete those that are stale, and remove their lines.")
        if name != MEMORY_INDEX and len(text) > MEMORY_MAX_CHARS:
            raise ValueError(
                f"{name} would have {len(text)} characters; a memory holds one fact, in at "
                f"most {MEMORY_MAX_CHARS}. Cut it down, or split it into several memories.")
        seen[name] = folder.write(name, text).text

    def absent(name: str) -> None:
        try:
            folder.read(name)
        except KeyError:
            return
        raise ValueError(f"{name} exists. View it and change it with str_replace or insert, "
                         "or delete it first.")

    match command:
        case "view" if path in ("", ".", "/"):
            files = folder.files()
            return "\n".join(f"{f.path} ({len(f.text)} characters)" for f in files) or (
                "The memory folder is empty.")
        case "view":
            seen[path] = read(path)
            lines = seen[path].splitlines()
            return "\n".join(f"{i:6}\t{line}" for i, line in enumerate(lines, 1)) or "(empty)"
        case "create":
            needs("path", "file_text")
            absent(path)
            write(path, args["file_text"])
            return f"Saved {path}."
        case "str_replace":
            needs("path", "old_str", "new_str")
            text = known(path)
            count = text.count(args["old_str"])
            if count != 1:
                raise ValueError(f"old_str occurs {count} times in {path}; it must occur once.")
            write(path, text.replace(args["old_str"], args["new_str"]))
            return f"Edited {path}."
        case "insert":
            needs("path", "insert_line", "insert_text")
            lines = known(path).splitlines(keepends=True)
            at = args["insert_line"]
            if at > len(lines):
                raise ValueError(f"{path} has {len(lines)} lines.")
            added = args["insert_text"] if args["insert_text"].endswith("\n") else (
                args["insert_text"] + "\n")
            if at and not lines[at - 1].endswith("\n"):
                lines[at - 1] += "\n"
            write(path, "".join([*lines[:at], added, *lines[at:]]))
            return f"Inserted into {path}."
        case "delete":
            needs("path")
            known(path)
            folder.delete(path)
            del seen[path]
            return f"Deleted {path}."
        case "rename":
            needs("old_path", "new_path")
            old, new = args["old_path"].strip(), args["new_path"].strip()
            text = known(old)
            absent(new)
            write(new, text)
            folder.delete(old)
            del seen[old]
            return f"Renamed {old} to {new}."
    raise ValueError(f"Unknown command {command!r}.")
