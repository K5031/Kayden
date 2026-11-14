"""MCP servers in the settings: saved like module settings, their variables in the keychain,
built at the next start as a second Tools module."""

from __future__ import annotations

import tomllib
from typing import Any

import pytest

import kadence.__main__ as launcher
from kadence.api import McpServer, SettingsUpdate
from kadence.runtime import ModuleState
from tests.conftest import Harness

RUNNING = {"core": "first", "memory": "fake-memory", "tools": "fake-tools",
           "frontend": "scripted"}
GITHUB = McpServer("github", command="npx -y @modelcontextprotocol/server-github",
                   env={"GITHUB_TOKEN": "ghp-secret"})
DOCS = McpServer("docs", url="https://docs.example.com/mcp")


@pytest.fixture(autouse=True)
def _modules(installed):
    """The fake modules are installed for every test here."""


async def setup(make: Any, servers: tuple[McpServer, ...] = ()) -> Harness:
    state = ModuleState(running=RUNNING, work_folder="/start", mcp_servers=servers)
    return await make(modules=state)


def saved(h: Harness) -> dict[str, Any]:
    return tomllib.loads((h.data_dir / "settings.toml").read_text())


async def test_servers_are_saved_with_their_variables_in_the_keychain(make, keychain):
    h = await setup(make)
    await h.start()  # opens the event log
    s = await h.runtime.update_settings(SettingsUpdate(mcp_servers=[GITHUB, DOCS]))
    assert saved(h)["mcp_servers"] == [
        {"name": "github", "command": GITHUB.command, "env": ["GITHUB_TOKEN"]},
        {"name": "docs", "url": DOCS.url}]
    assert keychain.store[("kadence", "mcp/github/GITHUB_TOKEN")] == "ghp-secret"
    assert "ghp-secret" not in (h.data_dir / "settings.toml").read_text()
    assert "ghp-secret" not in (h.data_dir / "events.jsonl").read_text()
    assert [srv.name for srv in s.mcp_servers] == ["github", "docs"]
    assert s.mcp_servers[0].env == {"GITHUB_TOKEN": ""}  # values are never returned
    assert s.restart_needed  # servers start with the next start


async def test_an_empty_value_keeps_the_saved_one_and_removal_erases_it(make, keychain):
    h = await setup(make)
    await h.runtime.update_settings(SettingsUpdate(mcp_servers=[GITHUB]))
    kept = McpServer("github", command=GITHUB.command, env={"GITHUB_TOKEN": ""})
    await h.runtime.update_settings(SettingsUpdate(mcp_servers=[kept]))
    assert keychain.store[("kadence", "mcp/github/GITHUB_TOKEN")] == "ghp-secret"
    await h.runtime.update_settings(SettingsUpdate(mcp_servers=[]))
    assert ("kadence", "mcp/github/GITHUB_TOKEN") not in keychain.store
    assert saved(h)["mcp_servers"] == []


async def test_unchanged_servers_need_no_restart(make):
    h = await setup(make, servers=(McpServer("docs", url=DOCS.url),))
    s = await h.runtime.update_settings(SettingsUpdate(mcp_servers=[DOCS]))
    assert not s.restart_needed


@pytest.mark.parametrize("servers, message", [
    ([DOCS, DOCS], "twice"),
    ([McpServer("both", command="x", url="https://x")], "a command or a URL"),
    ([McpServer("neither")], "a command or a URL"),
    ([McpServer("bad name!", url="https://x")], "name"),
    ([McpServer("files", url="file:///etc")], "http"),
    ([McpServer("quote", command="say 'unclosed")], "command"),
    ([McpServer("env", url="https://x", env={"BAD-NAME": "v"})], "variable"),
    ([McpServer("env", url="https://x", env={"TOKEN": ""})], "value"),
])
async def test_invalid_servers_change_nothing(make, servers, message, keychain):
    h = await setup(make)
    with pytest.raises(ValueError, match=message):
        await h.runtime.update_settings(SettingsUpdate(mcp_servers=servers))
    assert not (h.data_dir / "settings.toml").exists() and not keychain.store


def test_saved_servers_become_an_mcp_tools_module(tmp_path, keychain):
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.toml").write_text(
        '[[mcp_servers]]\nname = "github"\ncommand = "npx server-github"\n'
        'env = ["GITHUB_TOKEN"]\n\n[[mcp_servers]]\nname = "docs"\nurl = "https://d/mcp"\n')
    keychain.store[("kadence", "mcp/github/GITHUB_TOKEN")] = "ghp-secret"
    config = launcher.load_config(launcher.parse_args(["--data-dir", str(data)]))
    [mcp] = config.extra_tools
    assert mcp.factory == "kadence.tools.mcp:factory"
    assert mcp.options["servers"] == [
        {"name": "github", "command": "npx server-github", "env": {"GITHUB_TOKEN": "ghp-secret"}},
        {"name": "docs", "url": "https://d/mcp", "env": {}}]
    assert [s.env for s in config.runtime.modules.mcp_servers] == [{"GITHUB_TOKEN": ""}, {}]
    assert "ghp-secret" not in launcher.show_config(config, data / "config.toml")


def test_no_servers_no_module(tmp_path):
    config = launcher.load_config(launcher.parse_args(["--data-dir", str(tmp_path)]))
    assert config.extra_tools == ()
