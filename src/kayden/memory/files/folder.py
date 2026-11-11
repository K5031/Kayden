"""A project's long-term memory as a folder of Markdown files, as Claude Code keeps it: one
file per memory with a name, description and type in its frontmatter, and MEMORY.md, the
index every request sees. The agent writes it with the memory tool (files.tool).

Blocking: call these from a worker thread (asyncio.to_thread).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MEMORY_INDEX = "MEMORY.md"

DEFAULT_PROJECT = "default"  # the folder of conversations without a work folder


@dataclass(frozen=True, slots=True)
class MemoryFile:
    path: str  # relative to the folder
    text: str
    updated_at: float

# What every request is told about memory, before the index. Claude Code's rules.
RULES = f"""\
# Memory

You have a persistent memory for this project: a folder of files that later conversations
in it also see, which you read and write with the memory tool. {MEMORY_INDEX}, its index,
is shown below; open a memory file with the memory tool's view command when it is relevant.

Each memory is one file holding one fact, with frontmatter:

```markdown
---
name: <short-kebab-case-slug>
description: <one-line summary, used to decide relevance in later conversations>
metadata:
  type: user | feedback | project | reference
---

<the fact; for feedback and project, follow with **Why:** and **How to apply:** lines.
Link related memories with [[their-name]].>
```

- user: who the user is (role, expertise, preferences).
- feedback: guidance the user has given on how you should work, corrections and confirmed
  approaches alike; include the why.
- project: ongoing work, goals or constraints that the files and history do not show;
  write dates as absolute dates.
- reference: pointers to resources outside (addresses, dashboards, tickets).

After writing a file, add a one-line pointer to {MEMORY_INDEX}: `- [Title](file.md) — hook`.
{MEMORY_INDEX} holds one line per memory and nothing else.

Before saving, check for a file that already covers it and update that one rather than
writing a duplicate; delete memories that turn out wrong. Don't save what the project's
files, code or history already record, or what only matters to this conversation.
Memories reflect what was true when they were written: check a file, name or setting a
memory mentions before relying on it."""


def project_key(folder: str | None) -> str:
    """The memory folder's name for a work folder: its absolute path with / turned into -,
    as Claude Code names its project folders; DEFAULT_PROJECT without one."""
    if not folder:
        return DEFAULT_PROJECT
    return str(Path(folder).expanduser().resolve()).replace("/", "-")


class MemoryFolder:
    """The files of one project's memory, kept inside `root`."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, relative: str) -> Path:
        """The file `relative` names; ValueError when it is not a plain relative path inside
        the folder."""
        given = PurePosixPath(relative.strip())
        parts = [p for p in given.parts if p != "."]
        if not parts or given.is_absolute() or ".." in parts:
            raise ValueError(f"{relative!r} is not a path inside the memory folder")
        return self.root.joinpath(*parts)

    def files(self) -> list[MemoryFile]:
        if not self.root.is_dir():
            return []
        found = sorted(p for p in self.root.rglob("*") if p.is_file())
        found.sort(key=lambda p: p.relative_to(self.root).as_posix() != MEMORY_INDEX)
        return [self._file(p) for p in found]

    def read(self, relative: str) -> MemoryFile:
        path = self.path(relative)
        if not path.is_file():
            raise KeyError(f"no memory file {relative}")
        return self._file(path)

    def write(self, relative: str, text: str) -> MemoryFile:
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return self._file(path)

    def delete(self, relative: str) -> None:
        path = self.path(relative)
        if not path.is_file():
            raise KeyError(f"no memory file {relative}")
        path.unlink()
        for parent in path.parents:  # leave no empty folders behind
            if parent == self.root or not parent.is_relative_to(self.root):
                break
            if any(parent.iterdir()):
                break
            parent.rmdir()

    def index(self) -> str:
        """MEMORY_INDEX's text, empty when there is none."""
        try:
            return (self.root / MEMORY_INDEX).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _file(self, path: Path) -> MemoryFile:
        return MemoryFile(path.relative_to(self.root).as_posix(),
                          path.read_text(encoding="utf-8"), path.stat().st_mtime)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """A memory file's top-level frontmatter fields (`metadata.type` as `type`) and its body;
    no fields and the whole text when there is no frontmatter."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, str] = {}
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return fields, "\n".join(lines[i + 1:]).lstrip("\n")
        key, _, value = line.partition(":")
        if value.strip():
            fields[key.strip()] = value.strip()
    return {}, text  # never closed: not frontmatter


def join_frontmatter(name: str, description: str, kind: str, body: str) -> str:
    """A memory file in Claude Code's format."""
    return (f"---\nname: {name}\ndescription: {description}\nmetadata:\n  type: {kind}\n"
            f"---\n\n{body.strip()}\n")
