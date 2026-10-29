"""End-to-end wiring: `python -m kayden` builds SqliteMemory, the Anthropic Core,
ShellFilesTools rooted at the current directory and the TUI. Without ANTHROPIC_API_KEY the
Core is replaced by the scripted fake; everything else is the real module."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input

from kayden.__main__ import build_config, main
from kayden.api import (
    AgentState,
    Core,
    Decision,
    Frontend,
    InstancePaths,
    Memory,
    Mode,
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
from kayden.frontends.tui import AssistantEntry, KaydenApp, PermissionPrompt, TuiFrontend
from kayden.memory_sqlite import SqliteMemory
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


def test_main_requires_an_api_key(workspace, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        main(["--data-dir", "data"])


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
