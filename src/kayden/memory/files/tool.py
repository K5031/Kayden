"""The memory tool: the agent reads and writes its project's memory folder with the commands
of Anthropic's memory tool, which Claude models know. Blocking: run from a worker thread."""

from __future__ import annotations

from typing import Any

from kayden.api import ToolSpec
from kayden.memory.files.folder import MEMORY_INDEX, MemoryFolder

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


def run_command(folder: MemoryFolder, args: dict[str, Any]) -> str:
    """Run one memory command; KeyError or ValueError say what is wrong."""
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

    match command:
        case "view" if path in ("", ".", "/"):
            files = folder.files()
            return "\n".join(f"{f.path} ({len(f.text)} characters)" for f in files) or (
                "The memory folder is empty.")
        case "view":
            lines = read(path).splitlines()
            return "\n".join(f"{i:6}\t{line}" for i, line in enumerate(lines, 1)) or "(empty)"
        case "create":
            needs("path", "file_text")
            folder.write(path, args["file_text"])
            return f"Saved {path}."
        case "str_replace":
            needs("path", "old_str", "new_str")
            text = read(path)
            count = text.count(args["old_str"])
            if count != 1:
                raise ValueError(f"old_str occurs {count} times in {path}; it must occur once.")
            folder.write(path, text.replace(args["old_str"], args["new_str"]))
            return f"Edited {path}."
        case "insert":
            needs("path", "insert_line", "insert_text")
            lines = read(path).splitlines(keepends=True)
            at = args["insert_line"]
            if at > len(lines):
                raise ValueError(f"{path} has {len(lines)} lines.")
            added = args["insert_text"] if args["insert_text"].endswith("\n") else (
                args["insert_text"] + "\n")
            if at and not lines[at - 1].endswith("\n"):
                lines[at - 1] += "\n"
            folder.write(path, "".join([*lines[:at], added, *lines[at:]]))
            return f"Inserted into {path}."
        case "delete":
            needs("path")
            folder.delete(path)
            return f"Deleted {path}."
        case "rename":
            needs("old_path", "new_path")
            text = read(args["old_path"])
            folder.write(args["new_path"], text)
            folder.delete(args["old_path"])
            return f"Renamed {args['old_path']} to {args['new_path']}."
    raise ValueError(f"Unknown command {command!r}.")
