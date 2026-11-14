"""Claude Code-style memory: the SQLite archive (kadence.memory.sqlite.SqliteArchive), with
long-term memory as a folder of Markdown files per project that the agent writes itself
with the memory tool (Agent Note: memory like Claude Code)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from os import PathLike
from pathlib import Path, PurePosixPath
from typing import Any

from kadence.api import (
    ConversationId,
    ConversationInfo,
    InstancePaths,
    MemoryNote,
    ToolCall,
    ToolContext,
    ToolOutput,
    ToolSpec,
)
from kadence.memory.files.folder import (
    MEMORY_INDEX,
    RULES,
    MemoryFile,
    MemoryFolder,
    join_frontmatter,
    loaded_index,
    project_key,
    split_frontmatter,
)
from kadence.memory.files.tool import MEMORY, run_command
from kadence.memory.sqlite import CUT_HALF, CompactionConfig, LongTerm, SqliteArchive
from kadence.memory.tools import memory_tools

__all__ = ["FilesMemory", "factory"]


def factory(options: dict[str, Any], paths: InstancePaths) -> FilesMemory:
    """Options: `path`, the archive (default `<data_dir>/memory-files.db`), `projects`, the
    folder of the projects' memory folders (default `<data_dir>/projects`), plus any
    CompactionConfig field."""
    options = dict(options)
    path = Path(options.pop("path", paths.data_dir / "memory-files.db"))
    projects = Path(options.pop("projects", paths.data_dir / "projects"))
    return FilesMemory(path, CompactionConfig(**options), projects=projects)


factory.display_name = "Claude Code-style memory (files per project)"  # type: ignore[attr-defined]


class FilesMemory(SqliteArchive):
    """One memory folder per project, `<projects>/<key>/memory/` (folder.project_key). Every
    request sees Claude Code's memory rules and the folder's MEMORY.md; the agent reads and
    writes the files with the memory tool, and nothing is saved without it. To the user, each
    file is a note: its path is the title, its frontmatter description the description, its
    body the text."""

    def __init__(self, path: str | PathLike[str], config: CompactionConfig = CUT_HALF, *,
                 projects: str | PathLike[str]) -> None:
        super().__init__(path, config)
        self._projects = Path(projects)
        # The index each conversation started with, and the summary it had then.
        self._loaded: dict[ConversationId, tuple[str, list[str]]] = {}
        # What each conversation has read of its folder: path -> text (tool.run_command).
        self._seen: dict[ConversationId, dict[str, str]] = {}
        self._keys: dict[str | None, str] = {}  # work folder -> its project's folder name

    async def _folder(self, folder: str | None) -> MemoryFolder:
        """The memory folder of a work folder's project (folder.project_key), found once:
        finding the repository it belongs to reads the disk."""
        key = self._keys.get(folder)
        if key is None:
            key = self._keys[folder] = await asyncio.to_thread(project_key, folder)
        return MemoryFolder(self._projects / key / "memory")

    async def _long_term(self, info: ConversationInfo, summary: str) -> LongTerm:
        """The index as it was when the conversation started here, as Claude Code loads
        memory once per session: a memory saved meanwhile is in the conversation itself
        (the agent's memory calls), and the start of every request stays the same, which
        keeps a provider's prompt cache valid. Read again when the summary changes, since
        the request's start changes then anyway."""
        loaded = self._loaded.get(info.id)
        if loaded is None or loaded[0] != summary:
            index = await asyncio.to_thread((await self._folder(info.folder)).index)
            loaded = (summary, loaded_index(index))
            self._loaded[info.id] = loaded
        return LongTerm(RULES, loaded[1], _render)

    async def delete_conversation(self, conversation_id: ConversationId) -> None:
        await super().delete_conversation(conversation_id)
        self._loaded.pop(conversation_id, None)
        self._seen.pop(conversation_id, None)

    # ── The agent's tool ──────────────────────────────────────────────────

    async def list_tools(self) -> Sequence[ToolSpec]:
        return (*memory_tools(editable=False), MEMORY)

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutput:
        if call.name != MEMORY.name:
            return await super().execute(call, ctx)
        info = await self._conversation(ctx.conversation_id)
        folder = await self._folder(info.folder)
        seen = self._seen.setdefault(info.id, {})

        def run() -> str:
            # The index its prompt shows counts as read while that is still the index.
            shown = self._loaded.get(info.id)
            if MEMORY_INDEX not in seen and shown is not None:
                index = folder.index()
                if index and loaded_index(index) == shown[1]:
                    seen[MEMORY_INDEX] = index
            return run_command(folder, dict(call.arguments), seen)

        try:
            return ToolOutput(await asyncio.to_thread(run))
        except (KeyError, ValueError) as e:
            return ToolOutput(str(e.args[0] if e.args else e), is_error=True)

    # ── The user's side: each file a note ─────────────────────────────────

    async def notes(self, *, folder: str | None = None) -> Sequence[MemoryNote]:
        files = await asyncio.to_thread((await self._folder(folder)).files)
        return [_note(f) for f in files]

    async def save_note(self, title: str, description: str, text: str, *,
                        note_id: str | None = None,
                        conversation_id: ConversationId | None = None,
                        folder: str | None = None) -> MemoryNote:
        """`title` is the file's path (".md" added when it has no suffix). The index is kept
        as written; another file gets Claude Code's frontmatter with this description, its
        name and type kept or made from the path."""
        path = title.strip()
        if path and not PurePosixPath(path).suffix:
            path += ".md"
        files = await self._folder(folder)

        def write() -> MemoryFile:
            if note_id is not None:
                current = files.read(note_id)  # KeyError if unknown
                fields, _ = split_frontmatter(current.text)
            else:
                fields = {}
                try:
                    fields, _ = split_frontmatter(files.read(path).text)
                except (KeyError, ValueError):
                    pass
            if path == MEMORY_INDEX:
                content = text
            else:
                content = join_frontmatter(fields.get("name") or PurePosixPath(path).stem,
                                           description.strip() or fields.get("description", ""),
                                           fields.get("type", "user"), text)
            written = files.write(path, content)
            if note_id is not None and note_id != written.path:
                files.delete(note_id)
            return written

        return _note(await asyncio.to_thread(write))

    async def delete_note(self, note_id: str, *, folder: str | None = None) -> None:
        await asyncio.to_thread((await self._folder(folder)).delete, note_id)


def _render(rules: str, index: Sequence[str]) -> str:
    """The memory rules, then MEMORY.md's lines; nothing without the rules."""
    if not rules:
        return ""
    return f"{rules}\n\n## {MEMORY_INDEX}\n" + ("\n".join(index) if index
                                                else "(empty: nothing saved yet)")


def _note(file: MemoryFile) -> MemoryNote:
    fields, body = split_frontmatter(file.text)
    return MemoryNote(file.path, file.path, fields.get("description", ""), body, None,
                      file.updated_at)
