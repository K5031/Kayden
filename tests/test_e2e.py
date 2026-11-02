"""End-to-end wiring: `python -m kayden` builds the modules named in the config file, by
default SqliteMemory, the Anthropic Core, ShellFilesTools rooted at the current directory and
the TUI. Without an API key the Core is replaced by the scripted fake; everything else is the
real module."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input

from kayden.__main__ import build_config
from kayden.api import (
    AgentState,
    Budget,
    Core,
    Decision,
    Frontend,
    InstancePaths,
    Memory,
    Mode,
    PermissionPolicy,
    PermissionRequested,
    Role,
    StartOptions,
    Text,
    ToolCall,
    ToolResult,
    Tools,
    ToolStatus,
)
from kayden.core.anthropic import AnthropicCore
from kayden.core.openai_compat import OpenAICompatCore
from kayden.frontends.tui import AssistantEntry, KaydenApp, PermissionPrompt, TuiFrontend
from kayden.memory.sqlite import SqliteMemory
from kayden.runtime import ModuleSpec, Runtime, load_module, run_instance
from kayden.tools.shell_files import ShellFilesTools
from tests.fakes.core import ScriptedCore, Step, call

COMMAND = "echo wired > out.txt && cat out.txt"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def steps() -> list[Step]:
    return [Step(calls=[call("shell", {"command": COMMAND}, id="c1")]), Step(text="Done.")]


async def transcript(data_dir: Path) -> list[Any]:
    memory = SqliteMemory(data_dir / "memory.db")
    try:
        [conversation] = await memory.list_conversations(Mode.SESSION)
        return list(await memory.history(conversation.id))
    finally:
        await memory.aclose()


def assert_wired(messages: list[Any], workspace: Path) -> None:
    assert [m.role for m in messages] == [Role.USER, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT]
    assert messages[1].blocks == (ToolCall("c1", "shell", {"command": COMMAND}),)
    [result] = messages[2].blocks
    assert isinstance(result, ToolResult) and result.status is ToolStatus.OK
    assert "wired" in result.content
    assert messages[3].blocks == (Text("Done."),)
    assert (workspace / "out.txt").read_text() == "wired\n"


def test_build_config_wires_the_real_modules(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config = build_config(["--data-dir", str(tmp_path / "data"), "--mode", "session",
                           "--permissions", "allow_read_only"])
    assert config.runtime.data_dir == tmp_path / "data"
    assert config.runtime.permissions == "allow_read_only"
    paths = InstancePaths(config.runtime.data_dir)
    expected = [(config.core, Core, AnthropicCore), (config.memory, Memory, SqliteMemory),
                (config.tools, Tools, ShellFilesTools), (config.frontend, Frontend, TuiFrontend)]
    for spec, kind, cls in expected:
        assert isinstance(load_module(spec, kind, paths), cls)
    tools = load_module(config.tools, Tools, paths)
    assert tools.root == workspace.resolve()
    frontend = load_module(config.frontend, Frontend, paths)
    assert frontend.start_options == StartOptions(Mode.SESSION)


DEEPSEEK_CORE = """
[core]
factory = "kayden.core.openai_compat:factory"
options = { profile = "deepseek" }
"""


def test_config_file_in_the_data_dir_selects_the_core(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.toml").write_text(DEEPSEEK_CORE)
    config = build_config(["--data-dir", str(data_dir)])
    assert config.core == ModuleSpec("kayden.core.openai_compat:factory", {"profile": "deepseek"})
    assert isinstance(load_module(config.core, Core, InstancePaths(data_dir)), OpenAICompatCore)
    default = build_config(["--data-dir", str(tmp_path / "data")])
    assert default.memory == ModuleSpec("kayden.memory.sqlite:factory")
    assert default.tools == ModuleSpec("kayden.tools.shell_files:factory",
                                       {"root": str(workspace)})
    assert default.frontend == ModuleSpec("kayden.frontends.tui:factory")


def test_explicit_config_file_wins_over_the_data_dir(workspace, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.toml").write_text(DEEPSEEK_CORE)
    explicit = tmp_path / "other.toml"
    explicit.write_text('[core]\nfactory = "kayden.core.anthropic:factory"\n'
                        '[tools]\noptions = { root = "/srv", shell_timeout = 5 }\n')
    config = build_config(["--data-dir", str(data_dir), "--config", str(explicit)])
    assert config.core == ModuleSpec("kayden.core.anthropic:factory")
    assert config.tools == ModuleSpec("kayden.tools.shell_files:factory",
                                      {"root": "/srv", "shell_timeout": 5})


def test_command_line_start_options_override_the_frontend_options(workspace, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[frontend]\noptions = { mode = "continuous", model = "m" }\n')
    config = build_config(["--data-dir", str(tmp_path), "--config", str(path),
                           "--mode", "session"])
    assert config.frontend.options == {"mode": "session", "model": "m",
                                       "activation": "user_controlled"}


RUNTIME = """
[runtime]
model = "deepseek-flash"
maintenance_model = "deepseek-flash"
permissions = "deny"

[runtime.budget]
max_cost_usd = 2.5
max_tokens = 1000000
max_duration_s = 600
max_tool_calls = 50
"""


def test_runtime_settings_come_from_the_config_file(workspace, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(RUNTIME)
    runtime = build_config(["--data-dir", str(tmp_path), "--config", str(path)]).runtime
    assert runtime.model == runtime.maintenance_model == "deepseek-flash"
    assert runtime.permissions is PermissionPolicy.DENY
    assert runtime.budget == Budget(max_tokens=1_000_000, max_cost_usd=2.5,
                                    max_duration_s=600, max_tool_calls=50)


def test_command_line_flags_override_the_runtime_section(workspace, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(RUNTIME)
    runtime = build_config(["--data-dir", str(tmp_path), "--config", str(path),
                            "--model", "deepseek-v4-pro", "--maintenance-model", "m",
                            "--permissions", "ask"]).runtime
    assert (runtime.model, runtime.maintenance_model) == ("deepseek-v4-pro", "m")
    assert runtime.permissions is PermissionPolicy.ASK
    assert runtime.budget.max_cost_usd == 2.5


def test_runtime_defaults_without_a_runtime_section(workspace, tmp_path):
    runtime = build_config(["--data-dir", str(tmp_path)]).runtime
    assert runtime.model is None and runtime.maintenance_model is None
    assert runtime.permissions is PermissionPolicy.ASK
    assert runtime.budget == Budget()


@pytest.mark.parametrize(("content", "message"), [
    ("[runtime]\nretries = 3\n", "unknown key 'retries' in [runtime]"),
    ("[runtime]\nmodel = 3\n", "[runtime] model must be a string"),
    ("[runtime]\npermissions = 'yes'\n", "[runtime] permissions must be one of"),
    ("[runtime]\nbudget = 5\n", "[runtime.budget] must be a table"),
    ("[runtime.budget]\nmax_usd = 5\n", "unknown key 'max_usd' in [runtime.budget]"),
    ("[runtime.budget]\nmax_tokens = 1.5\n", "max_tokens must be a non-negative integer"),
    ("[runtime.budget]\nmax_tool_calls = true\n",
     "max_tool_calls must be a non-negative integer"),
    ("[runtime.budget]\nmax_cost_usd = -1\n", "max_cost_usd must be a non-negative number"),
    ("[runtime.budget]\nmax_duration_s = 'x'\n",
     "max_duration_s must be a non-negative number"),
    ("[agent]\n", "unknown section 'agent'"),
    ("[core]\nmodel = 'x'\n", "unknown key 'model' in [core]"),
    ("[core]\nfactory = 3\n", "[core] factory must be a string"),
    ("[core]\noptions = 'x'\n", "[core] options must be a table"),
    ("core = 'x'\n", "[core] must be a table"),
    ("[core\n", "invalid TOML"),
])
def test_invalid_config_files_are_refused(workspace, tmp_path, content, message):
    path = tmp_path / "config.toml"
    path.write_text(content)
    with pytest.raises(SystemExit, match=re.escape(message)):
        build_config(["--data-dir", str(tmp_path), "--config", str(path)])


def test_missing_explicit_config_file_is_refused(workspace, tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        build_config(["--data-dir", str(tmp_path), "--config", str(tmp_path / "none.toml")])


def test_module_entry_point_help():
    out = subprocess.run([sys.executable, "-m", "kayden", "--help"], capture_output=True,
                         text=True, check=True, env={**os.environ, "ANTHROPIC_API_KEY": ""})
    assert "--data-dir" in out.stdout


async def test_instance_runs_on_the_scripted_core(workspace, tmp_path):
    async def script(runtime, frontend) -> None:
        recorder = frontend.recorder
        await runtime.start(StartOptions(Mode.SESSION))
        await runtime.send("make the file")
        [request] = await recorder.wait_for(lambda r: r.of(PermissionRequested))
        await runtime.resolve_permission(request.request.id, Decision.ALLOW_ONCE)
        await recorder.wait_state(AgentState.IDLE, count=2)  # after start, after the turn

    data_dir = tmp_path / "data"
    config = build_config(["--data-dir", str(data_dir)])
    config = replace(
        config,
        core=ModuleSpec("tests.fakes.core:factory", {"steps": steps()}),
        frontend=ModuleSpec("tests.fakes.frontend:factory", {"script": script}),
    )
    await asyncio.wait_for(run_instance(config), 20)
    assert_wired(await transcript(data_dir), workspace)
    assert (data_dir / "usage-session.json").exists()


async def test_tui_drives_the_real_runtime(workspace, tmp_path):
    data_dir = tmp_path / "data"
    config = build_config(["--data-dir", str(data_dir)])
    paths = InstancePaths(data_dir)
    runtime = Runtime(core=ScriptedCore(steps()), memory=load_module(config.memory, Memory, paths),
                      tools=load_module(config.tools, Tools, paths),
                      frontend=TuiFrontend().info, config=config.runtime)
    app = KaydenApp(runtime, StartOptions(Mode.SESSION))
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            async def until(predicate) -> None:
                while not predicate():
                    await pilot.pause(0.02)

            await asyncio.wait_for(until(lambda: app.conversation is not None), 5)
            app.query_one("#prompt", Input).value = "make the file"
            await pilot.press("enter")
            await asyncio.wait_for(until(lambda: bool(app.query(PermissionPrompt))), 5)
            await pilot.click(f"#{Decision.ALLOW_ONCE.value}")
            await asyncio.wait_for(until(lambda: any(
                e.final and e.body == "Done." for e in app.query(AssistantEntry))), 10)
    finally:
        await runtime.aclose()
    assert_wired(await transcript(data_dir), workspace)
