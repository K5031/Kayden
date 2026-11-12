"""A project's long-term memory as a folder of Markdown files, as Claude Code keeps it: one
file per memory with a name, description and type in its frontmatter, and MEMORY.md, the
index every request sees. The agent writes it with the memory tool (files.tool).

Blocking: call these from a worker thread (asyncio.to_thread).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

MEMORY_INDEX = "MEMORY.md"
# What of MEMORY_INDEX is loaded into a conversation, as in Claude Code: its first lines
# or bytes, whichever ends first.
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25 * 1024

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
writing a duplicate; delete memories that turn out wrong. View a memory before you change
it, and change it with a small edit rather than by writing it again. Only the first
{INDEX_MAX_LINES} lines of {MEMORY_INDEX} are loaded: keep one line per memory, move detail
into the memory files, and merge or drop stale entries. Don't save what the project's
files, code or history already record, or what only matters to this conversation.
Memories reflect what was true when they were written: check a file, name or setting a
memory mentions before relying on it."""


def loaded_index(text: str) -> list[str]:
    """The index's lines as a conversation gets them: non-empty, within both limits."""
    lines, size = [], 0
    for line in text.splitlines()[:INDEX_MAX_LINES]:
        size += len(line.encode()) + 1
        if size > INDEX_MAX_BYTES:
            break
        if line.strip():
            lines.append(line)
    return lines


def project_root(folder: Path) -> Path:
    """The folder a project's memory is named after, as in Claude Code: the git repository
    `folder` is in, the main one when it is a worktree, so that every folder and worktree
    of a repository shares one memory; `folder` itself outside any repository. Blocking."""
    folder = folder.expanduser().resolve()
    for candidate in (folder, *folder.parents):
        git = candidate / ".git"
        if git.is_dir():
            return candidate
        if git.is_file():  # a worktree or a submodule: "gitdir: <where its git data is>"
            try:
                pointer = git.read_text(encoding="utf-8").strip()
                gitdir = (candidate / pointer.removeprefix("gitdir:").strip()).resolve()
                # A worktree's git data names the main repository's .git in `commondir`.
                main = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()
                        ).resolve()
            except OSError:
                return candidate
            return main.parent if main.name == ".git" else candidate
    return folder


def project_key(folder: str | None) -> str:
    """The memory folder's name for a work folder: the absolute path of its project_root
    with / turned into -, as Claude Code names its project folders; DEFAULT_PROJECT
    without one. Blocking."""
    if not folder:
        return DEFAULT_PROJECT
    return str(project_root(Path(folder))).replace("/", "-")


_MODIFIED = re.compile(r"\s+modified:")


def stamped(text: str, now: datetime | None = None) -> str:
    """A memory file with the time it is written in its frontmatter (`modified` under
    `metadata`), as Claude Code records it; as it is when it has no frontmatter."""
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if not lines or lines[0].strip() != "---" or end is None:
        return text
    when = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="milliseconds")
    stamp = f"  modified: {when.replace('+00:00', 'Z')}"
    front = lines[1:end]
    old = next((i for i, line in enumerate(front) if _MODIFIED.match(line)), None)
    if old is not None:
        front[old] = stamp
    elif "metadata:" in (line.rstrip() for line in front):
        at = [line.rstrip() for line in front].index("metadata:") + 1
        while at < len(front) and front[at].startswith((" ", "\t")):
            at += 1
        front.insert(at, stamp)
    else:
        front += ["metadata:", stamp]
    return "\n".join([lines[0], *front, *lines[end:]])


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
        path.write_text(text if path.name == MEMORY_INDEX and path.parent == self.root
                        else stamped(text), encoding="utf-8")
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
