"""A Tools module that offers the tools of MCP servers (Model Context Protocol), through the
official `mcp` SDK.

Each server is a command Kadence starts (talking over its stdin and stdout) or a URL. Every
server runs in a task of its own that holds its connection, so it is opened and closed in
the same task, as the SDK requires. A server that fails to start is left out and logged;
the others still work. Tools are named `mcp__<server>__<tool>` so their source is visible and
names from different servers never clash.

A command gets a small environment (the SDK's safe default: PATH, HOME and the like), the
variables configured for it, and PERMISSION_CEILING_ENV, so a Kadence it launches cannot
escape the ceiling. No call is confined: what a server does is unknown, so the runtime
reviews or asks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.client.stdio import get_default_environment

from kadence.api import (
    PERMISSION_CEILING_ENV,
    InstancePaths,
    ToolCall,
    ToolContext,
    ToolOutput,
    Tools,
    ToolSpec,
)

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 30.0  # seconds for a server to start and list its tools
CLOSE_TIMEOUT = 5.0
MAX_NAME = 64  # what model APIs accept for a tool name
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """One server: `command` (a shell-style command line) or `url`, and its variables."""

    name: str
    command: str | None = None
    url: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)

    def target(self) -> StdioServerParameters | str:
        if self.url:
            return self.url
        if not self.command:
            raise ValueError(f"MCP server {self.name!r} needs a command or a URL")
        argv = shlex.split(self.command)
        env = {**get_default_environment(), **self.env}
        if (ceiling := os.environ.get(PERMISSION_CEILING_ENV)) is not None:
            env[PERMISSION_CEILING_ENV] = ceiling
        return StdioServerParameters(command=argv[0], args=argv[1:], env=env)


def factory(options: dict[str, Any], paths: InstancePaths) -> McpTools:
    """Options: servers, a list of {name, command or url, env}."""
    servers = [ServerConfig(s["name"], s.get("command"), s.get("url"), dict(s.get("env", {})))
               for s in options.get("servers", [])]
    return McpTools({s.name: s.target() for s in servers})


factory.display_name = "MCP servers"  # type: ignore[attr-defined]


@dataclass
class _Server:
    name: str
    target: Any  # what mcp.Client connects to
    client: Client | None = None
    tools: dict[str, ToolSpec] = field(default_factory=dict)  # by Kadence's name
    originals: dict[str, str] = field(default_factory=dict)  # Kadence's name -> the server's
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class McpTools(Tools):
    """`servers` maps a server name to what `mcp.Client` connects to: StdioServerParameters,
    a URL, or (in tests) a server object."""

    def __init__(self, servers: Mapping[str, Any]) -> None:
        self._servers = [_Server(name, target) for name, target in servers.items()]
        self._started = False
        self._owner: dict[str, _Server] = {}

    async def list_tools(self) -> Sequence[ToolSpec]:
        if not self._started:
            self._started = True
            for server in self._servers:
                server.task = asyncio.create_task(self._serve(server))
            waiters = [asyncio.create_task(s.ready.wait()) for s in self._servers]
            if waiters:
                _, late = await asyncio.wait(waiters, timeout=CONNECT_TIMEOUT)
                for waiter in late:
                    waiter.cancel()
            for server in self._servers:
                if not server.ready.is_set():
                    logger.warning("MCP server %s did not start in %ss", server.name,
                                   CONNECT_TIMEOUT)
        specs = []
        for server in self._servers:
            for name, spec in server.tools.items():
                self._owner[name] = server
                specs.append(spec)
        return specs

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutput:
        server = self._owner.get(call.name)
        if server is None or server.client is None:
            return ToolOutput(f"Unknown tool: {call.name}", is_error=True)
        result = await server.client.call_tool(server.originals[call.name], call.arguments)
        return ToolOutput(_text(result), is_error=bool(result.is_error))

    async def aclose(self) -> None:
        for server in self._servers:
            server.stop.set()
        tasks = [s.task for s in self._servers if s.task is not None]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=CLOSE_TIMEOUT)
            for task in pending:
                task.cancel()

    async def _serve(self, server: _Server) -> None:
        """Open the connection, list the tools, hold it until aclose(), close it here."""
        try:
            async with Client(server.target) as client:
                listed, cursor = [], None
                while True:
                    page = await client.list_tools(cursor=cursor)
                    listed += page.tools
                    if not (cursor := page.next_cursor):
                        break
                for tool in listed:
                    name = _name(server.name, tool.name)
                    server.originals[name] = tool.name
                    server.tools[name] = ToolSpec(
                        name, _describe(server.name, tool), tool.input_schema or {"type": "object"},
                        read_only=bool(tool.annotations and tool.annotations.read_only_hint))
                server.client = client
                server.ready.set()
                await server.stop.wait()
        except Exception:
            logger.exception("MCP server %s failed", server.name)
        finally:
            server.client = None
            server.ready.set()


def _name(server: str, tool: str) -> str:
    return _UNSAFE.sub("_", f"mcp__{server}__{tool}")[:MAX_NAME]


def _describe(server: str, tool: Any) -> str:
    text = tool.description or tool.title or tool.name
    return f"{text} (from the MCP server {server!r})"


def _text(result: Any) -> str:
    parts = []
    for item in result.content:
        if getattr(item, "type", None) == "text":
            parts.append(item.text)
        else:
            parts.append(f"[{getattr(item, 'type', 'unknown')} content]")
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False, indent=1))
    return "\n".join(parts)
