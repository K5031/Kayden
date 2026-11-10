"""The MCP Tools module: tools of MCP servers, named by server, called through the SDK."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from kayden.api import PERMISSION_CEILING_ENV, InstancePaths, ToolCall
from kayden.tools.mcp import McpTools, ServerConfig, factory
from tests.conformance.tools_suite import RecordingContext
from tests.fakes.mcp_server import build


@pytest.fixture
async def tools() -> Any:
    module = McpTools({"notes": build()})
    yield module
    await module.aclose()


async def run(tools: McpTools, tool: str, **args: Any):
    return await tools.execute(ToolCall("c", tool, args), RecordingContext("c"))


async def test_tools_are_named_by_server(tools):
    specs = {s.name: s for s in await tools.list_tools()}
    assert set(specs) >= {"mcp__notes__add", "mcp__notes__broken", "mcp__notes__env"}
    add = specs["mcp__notes__add"]
    assert add.read_only and "Add two numbers" in add.description and "notes" in add.description
    assert add.parameters["properties"].keys() == {"a", "b"}
    assert not specs["mcp__notes__broken"].read_only


async def test_calls_return_text_and_errors(tools):
    await tools.list_tools()
    out = await run(tools, "mcp__notes__add", a=2, b=3)
    assert out.content == "5" and not out.is_error
    out = await run(tools, "mcp__notes__broken")
    assert out.is_error and "broken" in out.content  # the SDK hides the server's own error


async def test_no_call_is_confined(tools):
    await tools.list_tools()
    assert not await tools.confined(ToolCall("c", "mcp__notes__add", {"a": 1, "b": 1}))


async def test_a_server_that_fails_is_left_out():
    module = McpTools({"gone": ServerConfig("gone", "kayden-no-such-command").target(),
                       "notes": build()})
    try:
        names = {s.name for s in await module.list_tools()}
        assert names and all(n.startswith("mcp__notes__") for n in names)
    finally:
        await module.aclose()


def test_a_server_needs_a_command_or_url():
    with pytest.raises(ValueError, match="command or a URL"):
        ServerConfig("x").target()


async def test_a_command_server_gets_its_variables_and_the_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv(PERMISSION_CEILING_ENV, "ask")
    monkeypatch.setenv("KAYDEN_TEST_SECRET", "leaked")
    module = factory({"servers": [{
        "name": "notes", "command": f"{sys.executable} -m tests.fakes.mcp_server",
        "env": {"GREETING": "hello"}}]}, InstancePaths(tmp_path))
    try:
        assert await module.list_tools()
        assert (await run(module, "mcp__notes__env", name="GREETING")).content == "hello"
        assert (await run(module, "mcp__notes__env", name=PERMISSION_CEILING_ENV)).content == "ask"
        assert (await run(module, "mcp__notes__env", name="KAYDEN_TEST_SECRET")).content == (
            "(unset)")  # only the safe default environment, not Kayden's
    finally:
        await module.aclose()
