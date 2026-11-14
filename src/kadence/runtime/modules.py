"""Installed modules (entry points) and the module settings that apply at the next start."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import re
import shlex
import sys
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from kadence.api import MODULE_KINDS, McpServer, ModuleChoice, ModuleInfo, SettingsUpdate
from kadence.runtime.config import ModuleState, Restart
from kadence.runtime.settings import get_stored_key, has_key, save_settings, store_key

logger = logging.getLogger(__name__)

GROUPS = {"core": "kadence.cores", "memory": "kadence.memories", "tools": "kadence.tools",
          "frontend": "kadence.frontends"}
LABELS = {"core": "AI provider", "memory": "memory", "tools": "tools", "frontend": "frontend"}
TERMINAL, WINDOW = "terminal", "window"
MCP_FACTORY = "kadence.tools.mcp:factory"
_SERVER_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def mcp_key(server: str, variable: str) -> str:
    """The keychain entry of an MCP server's variable."""
    return f"mcp/{server}/{variable}"


def mcp_saved(server: McpServer) -> dict[str, object]:
    """How a server is written to the settings file: its variables by name only."""
    entry: dict[str, object] = {"name": server.name}
    entry.update({"command": server.command} if server.command else {"url": server.url})
    if server.env:
        entry["env"] = list(server.env)
    return entry


@dataclass(frozen=True, slots=True)
class Installed:
    id: str
    factory: str  # the entry point's target, "package.module:factory"
    name: str
    key_name: str | None
    kind: str | None  # frontends only


def targets(kind: str) -> dict[str, str]:
    """Entry point id -> factory path, from package metadata only (imports nothing)."""
    found: dict[str, str] = {}
    for ep in entry_points(group=GROUPS[kind]):
        found.setdefault(ep.name, ep.value)
    return found


def installed(kind: str) -> list[Installed]:
    """The modules of a kind whose entry point loads, by id. Blocks (imports them): call
    it off the event loop."""
    found = []
    for module_id, target in sorted(targets(kind).items()):
        if (module := load_entry(kind, module_id, target)) is not None:
            found.append(module)
    return found


def load_entry(kind: str, module_id: str, target: str) -> Installed | None:
    module_name, _, attr = target.partition(":")
    try:
        factory: Any = importlib.import_module(module_name)
        for part in attr.split("."):
            factory = getattr(factory, part)
    except Exception:
        logger.info("module %s %r (%s) does not load", kind, module_id, target, exc_info=True)
        return None
    return Installed(module_id, target, getattr(factory, "display_name", module_id),
                     getattr(factory, "key_name", None),
                     getattr(factory, "kind", TERMINAL) if kind == "frontend" else None)


def find(kind: str, module_id: str | None) -> Installed | None:
    """Blocks like installed()."""
    if module_id is None or (target := targets(kind).get(module_id)) is None:
        return None
    return load_entry(kind, module_id, target)


def has_display() -> bool:
    """Whether a window can be opened from this process."""
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class ModuleSettings:
    """The module settings behind Settings and SettingsUpdate: validated, saved and kept
    for the next start; nothing is built."""

    def __init__(self, data_dir: Path, state: ModuleState, frontend_kind: str,
                 startup_error: str | None = None, already_open: bool = False):
        self.data_dir = data_dir
        self.running = {kind: state.running.get(kind) for kind in MODULE_KINDS}
        self.chosen = {kind: state.chosen.get(kind, self.running[kind]) for kind in MODULE_KINDS}
        self.frontend_kind = frontend_kind
        self.startup_error = startup_error
        self.already_open = already_open
        self.work_folder = state.work_folder  # for new conversations; applies live
        self.key_saved = False
        self.mcp_running = tuple(state.mcp_servers)  # values ""; built for this run
        self.mcp_servers = self.mcp_running  # saved for the next start

    async def fields(self) -> dict[str, Any]:
        """The module part of Settings."""
        found = {kind: await asyncio.to_thread(installed, kind) for kind in MODULE_KINDS}
        modules = {}
        for kind in MODULE_KINDS:
            infos = [ModuleInfo(m.id, m.name, m.key_name, await has_key(m.key_name), m.kind)
                     for m in found[kind]]
            modules[kind] = ModuleChoice(self.running[kind], self.chosen[kind], infos)
        restart_needed = (self.key_saved or self.mcp_servers != self.mcp_running
                          or any(self.chosen[k] != self.running[k] for k in MODULE_KINDS))
        return {"modules": modules, "work_folder": self.work_folder,
                "mcp_servers": self.mcp_servers,
                "restart_needed": restart_needed,
                "restart_frontend": self._restart_frontend(found["frontend"]),
                "startup_error": self.startup_error, "already_open": self.already_open}

    def _restart_frontend(self, frontends: list[Installed]) -> str | None:
        running, chosen = self.running["frontend"], self.chosen["frontend"]
        target = next((f for f in frontends if f.id == chosen), None)
        if target is None or target.kind == self.frontend_kind:
            return running if target is None else chosen
        if self.frontend_kind == TERMINAL and target.kind == WINDOW and has_display():
            return chosen
        return running

    async def restart(self, conversation_id: str | None) -> Restart:
        frontend = (await self.fields())["restart_frontend"]
        kind = self.frontend_kind
        if frontend != self.running["frontend"]:
            info = await asyncio.to_thread(find, "frontend", frontend)
            kind = info.kind if info is not None and info.kind else kind
        if self.chosen["memory"] != self.running["memory"]:
            conversation_id = None
        return Restart(frontend, kind, conversation_id)

    async def prepare(self, u: SettingsUpdate) -> dict[str, Any]:
        """Validate the module part of `u`: what to save to the settings file. ValueError
        for an invalid part."""
        saved: dict[str, Any] = {}
        if u.modules:
            for kind, module_id in u.modules.items():
                if kind not in MODULE_KINDS:
                    raise ValueError(f"unknown module kind {kind!r}")
                if await asyncio.to_thread(find, kind, module_id) is None:
                    raise ValueError(f"no {LABELS[kind]} module {module_id!r} is installed")
            changed = {k: v for k, v in u.modules.items() if v != self.chosen[k]}
            if changed:
                saved["modules"] = changed
            if "core" in changed:
                saved["model"] = saved["maintenance_model"] = ""  # the new Core's first
        if u.api_key is not None:
            core = (u.modules or {}).get("core") or self.chosen["core"]
            info = await asyncio.to_thread(find, "core", core)
            if info is None or info.key_name is None:
                name = info.name if info is not None else "this AI provider"
                raise ValueError(f"{name} takes no API key")
            if not u.api_key.strip():
                raise ValueError("the API key is empty")
        if u.mcp_servers is not None:
            await self._check_servers(u.mcp_servers)
            if [mcp_saved(s) for s in u.mcp_servers] != [mcp_saved(s) for s in self.mcp_servers]:
                saved["mcp_servers"] = [mcp_saved(s) for s in u.mcp_servers]
        if u.work_folder is not None:
            if self.work_folder is None:
                raise ValueError("the work folder of these tools cannot be changed")
            folder = Path(u.work_folder).expanduser().resolve()
            if not folder.is_dir():
                raise ValueError(f"{u.work_folder} is not a folder")
            if str(folder) != self.work_folder:
                saved["work_folder"] = str(folder)
        return saved

    async def _check_servers(self, servers: list[McpServer] | tuple[McpServer, ...]) -> None:
        names = [s.name for s in servers]
        for server in servers:
            if not _SERVER_NAME.fullmatch(server.name):
                raise ValueError(f"MCP server name {server.name!r}: use letters, digits, - "
                                 "and _ (at most 32)")
            if names.count(server.name) > 1:
                raise ValueError(f"the MCP server {server.name!r} is listed twice")
            if bool(server.command) == bool(server.url):
                raise ValueError(f"the MCP server {server.name!r} needs a command or a URL, "
                                 "not both")
            if server.url and not server.url.startswith(("http://", "https://")):
                raise ValueError(f"the URL of {server.name!r} must start with http:// or "
                                 "https://")
            if server.command:
                try:
                    if not shlex.split(server.command):
                        raise ValueError
                except ValueError:
                    raise ValueError(f"the command of {server.name!r} cannot be read "
                                     "(check its quotes)") from None
            for variable, value in server.env.items():
                if not _VARIABLE.fullmatch(variable):
                    raise ValueError(f"{variable!r} is not a variable name")
                if not value and await get_stored_key(mcp_key(server.name, variable)) is None:
                    raise ValueError(f"give a value for {variable} of {server.name!r}")

    async def _store_server_keys(self, servers: list[McpServer] | tuple[McpServer, ...]
                                 ) -> None:
        """Values given now go to the keychain; variables no longer listed leave it."""
        for server in servers:
            for variable, value in server.env.items():
                if value:
                    await store_key(mcp_key(server.name, variable), value)
                    self.key_saved = True
        kept = {mcp_key(s.name, v) for s in servers for v in s.env}
        for server in self.mcp_servers:
            for variable in server.env:
                if (key := mcp_key(server.name, variable)) not in kept:
                    await store_key(key, None)
        self.mcp_servers = tuple(
            McpServer(s.name, s.command, s.url, dict.fromkeys(s.env, "")) for s in servers)

    async def commit(self, u: SettingsUpdate, saved: dict[str, Any]) -> None:
        """Store the key, then save; on failure the keychain entry is restored."""
        key_name = None
        if u.api_key is not None:
            core = (u.modules or {}).get("core") or self.chosen["core"]
            info = await asyncio.to_thread(find, "core", core)
            assert info is not None and info.key_name is not None
            key_name = info.key_name
            old_key = await get_stored_key(key_name)
            await store_key(key_name, u.api_key)
        try:
            if saved:
                await asyncio.to_thread(save_settings, self.data_dir, saved,
                                        tables=("modules",))
        except BaseException:
            if key_name is not None:
                with contextlib.suppress(ValueError):
                    await store_key(key_name, old_key)
            raise
        self.chosen.update(saved.get("modules", {}))
        self.work_folder = saved.get("work_folder", self.work_folder)
        self.key_saved = self.key_saved or key_name is not None
        if u.mcp_servers is not None:
            await self._store_server_keys(u.mcp_servers)
