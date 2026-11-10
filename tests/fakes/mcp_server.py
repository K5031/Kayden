"""A small MCP server for the tests, run in process or as `python -m tests.fakes.mcp_server`."""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations


def build() -> MCPServer:
    server = MCPServer("notes")

    @server.tool(description="Add two numbers.",
                 annotations=ToolAnnotations(read_only_hint=True))
    def add(a: int, b: int) -> str:
        return str(a + b)

    @server.tool(description="Fail on purpose.")
    def broken() -> str:
        raise ValueError("this tool always fails")

    @server.tool(description="Show one environment variable.")
    def env(name: str) -> str:
        return os.environ.get(name, "(unset)")

    return server


if __name__ == "__main__":
    build().run()
