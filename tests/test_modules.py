"""Choosing modules in the app: discovery, saved choices at startup, the setup runtime,
and restarts (the swappable modules note)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import kayden.__main__ as launcher
from kayden.api import (
    RuntimeStateError,
    SettingsChanged,
    SettingsUpdate,
    ShuttingDown,
    StartOptions,
)
from kayden.frontends.desktop import launcher as desktop_launcher
from kayden.runtime import ModuleSpec, ModuleState, Restart, run_instance
from kayden.runtime.instance import InstanceLock
from kayden.runtime.modules import TERMINAL, WINDOW, installed
from tests.fakes import frontend as fake_frontend
from tests.fakes.frontend import Recorder, ScriptedFrontend
from tests.fakes.memory import InMemoryMemory

RUNNING = {"core": "first", "memory": "fake-memory", "tools": "fake-tools",
           "frontend": "scripted"}
FAKE_MODULES = '[modules]\ncore = "first"\nmemory = "fake-memory"\ntools = "fake-tools"\n'


@pytest.fixture(autouse=True)
def _modules(installed, clean_env, monkeypatch):
    monkeypatch.delenv("FIRST_API_KEY", raising=False)


def args(data_dir: Path, *extra: str) -> Any:
    return launcher.parse_args(["--data-dir", str(data_dir), *extra])


def settings_file(data_dir: Path, text: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.toml").write_text(text)


def with_display(monkeypatch: pytest.MonkeyPatch, display: bool) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    if display:
        monkeypatch.setenv("DISPLAY", ":0")
    else:
        monkeypatch.delenv("DISPLAY", raising=False)


def scripted(monkeypatch: pytest.MonkeyPatch, *scripts: Any) -> None:
    """The scripted frontends run these scripts, one per instance the launcher builds."""
    queue = list(scripts)

    def build(options: dict[str, Any], paths: Any, kind: str = "terminal") -> ScriptedFrontend:
        return ScriptedFrontend(queue.pop(0), kind=kind, options=options)

    monkeypatch.setattr(fake_frontend, "factory", build)
    monkeypatch.setattr(fake_frontend, "window_factory",
                        lambda options, paths: build(options, paths, "window"))


# ── Discovery ──


def test_installed_modules_are_the_entry_points_that_load():
    cores = {m.id: m for m in installed("core")}
    assert {"anthropic", "deepseek", "openai", "first", "second", "keyless"} == set(cores)
    assert (cores["first"].name, cores["first"].key_name) == ("First AI", "FIRST_API_KEY")
    assert (cores["keyless"].name, cores["keyless"].key_name) == ("keyless", None)
    frontends = {m.id: m.kind for m in installed("frontend")}
    assert frontends == {"tui": TERMINAL, "desktop": WINDOW, "scripted": TERMINAL,
                         "scripted-window": WINDOW}


async def test_an_installed_package_appears_in_settings(make):
    h = await make(modules=ModuleState(running=RUNNING))
    s = await h.runtime.settings()
    assert "scripted-window" in {m.id for m in s.modules["frontend"].installed}
    assert "fake-memory" in {m.id for m in s.modules["memory"].installed}


# ── Startup from saved choices ──


def test_saved_module_ids_pick_their_factories(tmp_path):
    settings_file(tmp_path, FAKE_MODULES)
    config = launcher.load_config(args(tmp_path))
    assert config.core.factory == "tests.fakes.core:first_factory"
    assert config.memory.factory == "tests.fakes.memory:factory"
    assert config.tools == ModuleSpec("tests.fakes.tools:factory")  # no root option
    assert config.frontend.factory == "kayden.frontends.tui:factory"
    assert dict(config.runtime.modules.running) == {**RUNNING, "frontend": "tui"}
    assert config.startup_error is None


def test_a_saved_provider_picks_the_core(tmp_path):
    settings_file(tmp_path, 'provider = "deepseek"\n')
    config = launcher.load_config(args(tmp_path))
    assert config.core.factory == "kayden.core.openai_compat:deepseek_factory"
    assert config.runtime.modules.running["core"] == "deepseek"


def test_config_options_stay_with_their_module(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[core]\nfactory = "tests.fakes.core:second_factory"\noptions = {models = ["x"]}\n')
    config = launcher.load_config(args(tmp_path))
    assert config.core.options == {"models": ["x"]}
    settings_file(tmp_path, '[modules]\ncore = "first"\n')
    assert launcher.load_config(args(tmp_path)).core == ModuleSpec(
        "tests.fakes.core:first_factory")


def test_import_paths_in_config_have_no_id(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[core]\nfactory = "kayden.core.openai_compat:factory"\n'
        '[frontend]\nfactory = "tests.fakes.core:x"\n')
    config = launcher.load_config(args(tmp_path))
    assert config.core.factory == "kayden.core.openai_compat:factory"
    assert config.frontend.factory == "tests.fakes.core:x"
    state = config.runtime.modules
    assert state.running["core"] is None and state.chosen["core"] is None
    assert state.running["frontend"] is None


def test_an_uninstalled_saved_module_is_a_startup_error(tmp_path):
    settings_file(tmp_path, '[modules]\nmemory = "gone"\n')
    config = launcher.load_config(args(tmp_path))
    assert config.startup_error == ("the memory module 'gone' is not installed; "
                                    "choose another one in Settings")
    assert config.runtime.modules.chosen["memory"] == "gone"


@pytest.mark.parametrize(("saved", "kind", "frontend"), [
    ("scripted-window", TERMINAL, "kayden.frontends.tui:factory"),
    ("scripted-window", WINDOW, "tests.fakes.frontend:window_factory"),
    ("scripted", TERMINAL, "tests.fakes.frontend:factory"),
    ("scripted", WINDOW, "kayden.frontends.desktop:factory"),
    ("gone", TERMINAL, "kayden.frontends.tui:factory"),
])
def test_each_launcher_runs_the_saved_frontend_of_its_kind(tmp_path, saved, kind, frontend):
    settings_file(tmp_path, f'[modules]\nfrontend = "{saved}"\n')
    config = launcher.load_config(args(tmp_path), launcher=kind)
    assert config.frontend.factory == frontend
    assert config.runtime.modules.chosen["frontend"] == saved
    assert config.startup_error is None


def test_a_restart_names_the_frontend_and_conversation(tmp_path):
    restart = Restart("scripted-window", WINDOW, "c1")
    config = launcher.load_config(args(tmp_path, "--conversation", "other"), launcher=WINDOW,
                                  restart=restart)
    assert config.frontend == ModuleSpec("tests.fakes.frontend:window_factory",
                                         {"start": True, "conversation_id": "c1"})


def test_a_cleared_model_overrides_the_config_file(tmp_path):
    (tmp_path / "config.toml").write_text('[runtime]\nmodel = "claude-x"\n')
    settings_file(tmp_path, 'model = ""\nmaintenance_model = ""\n')
    runtime = launcher.load_config(args(tmp_path)).runtime
    assert runtime.model is None and runtime.maintenance_model is None


def test_the_saved_work_folder_roots_the_tools(tmp_path):
    folder = tmp_path / "work"
    settings_file(tmp_path, f'work_folder = "{folder}"\n')
    config = launcher.load_config(args(tmp_path))
    assert config.tools.options == {"root": str(folder)}
    assert config.runtime.modules.work_folder == str(folder)


def test_the_desktop_launcher_opens_the_conversation_it_is_given(tmp_path):
    desktop_args = desktop_launcher.parse_args(["--data-dir", str(tmp_path),
                                                "--conversation", "c1"])
    config = launcher.load_config(desktop_args, launcher=WINDOW)
    assert config.frontend == ModuleSpec("kayden.frontends.desktop:factory",
                                         {"start": True, "conversation_id": "c1"})


# ── Restart ──


async def test_restart_shuts_down_and_names_the_open_conversation(make):
    h = await make(modules=ModuleState(running=RUNNING))
    info = await h.start()
    await h.runtime.update_settings(SettingsUpdate(modules={"core": "second"}))
    await h.runtime.restart()
    await h.rec.wait_for(lambda r: r.of(ShuttingDown))
    assert h.runtime.restart_request == Restart("scripted", TERMINAL, info.id)


async def test_a_memory_change_restarts_without_the_conversation(make):
    h = await make(modules=ModuleState(running=RUNNING))
    await h.start()
    await h.runtime.update_settings(SettingsUpdate(modules={"memory": "sqlite"}))
    await h.runtime.restart()
    assert h.runtime.restart_request == Restart("scripted", TERMINAL, None)


@pytest.mark.parametrize(("running_kind", "chosen", "display", "expected", "kind"), [
    (TERMINAL, "scripted-window", True, "scripted-window", WINDOW),
    (TERMINAL, "scripted-window", False, "scripted", TERMINAL),
    (WINDOW, "scripted", True, "scripted-window", WINDOW),
    (TERMINAL, "tui", False, "tui", TERMINAL),
])
async def test_restart_brings_up_the_chosen_frontend_where_it_can(
        make, monkeypatch, running_kind, chosen, display, expected, kind):
    with_display(monkeypatch, display)
    running = {**RUNNING, "frontend": "scripted" if running_kind == TERMINAL
               else "scripted-window"}
    h = await make(modules=ModuleState(running=running), kind=running_kind)
    s = await h.runtime.update_settings(SettingsUpdate(modules={"frontend": chosen}))
    assert s.restart_frontend == expected
    await h.runtime.restart()
    assert h.runtime.restart_request == Restart(expected, kind, None)


# ── Startup failures open the settings ──


async def test_a_missing_key_opens_the_frontend_and_is_entered_before_the_core_is_built(
        tmp_path, monkeypatch, keychain):
    """First run: no key yet. The frontend saves it on the setup runtime; Restart now
    builds the Core, which finds it."""
    settings_file(tmp_path, FAKE_MODULES + 'frontend = "scripted"\n')
    seen: dict[str, Any] = {}

    async def first_run(runtime: Any, frontend: ScriptedFrontend) -> None:
        s = await runtime.settings()
        seen["error"] = s.startup_error
        with pytest.raises(RuntimeStateError, match="FIRST_API_KEY is not set"):
            await runtime.start(StartOptions())
        s = await runtime.update_settings(SettingsUpdate(api_key="sk-1"))
        assert s.restart_needed
        await runtime.restart()

    async def second_run(runtime: Any, frontend: ScriptedFrontend) -> None:
        seen["key"] = runtime._core.options["key"]
        seen["started"] = await runtime.start(StartOptions())

    scripted(monkeypatch, first_run, second_run)
    assert await asyncio.wait_for(launcher.launch(args(tmp_path), TERMINAL), 10) is None
    assert seen["error"] == "tests.fakes.core:first_factory: FIRST_API_KEY is not set"
    assert keychain.store[("kayden", "FIRST_API_KEY")] == "sk-1"
    assert seen["key"] == "sk-1" and seen["started"].id


async def test_the_setup_runtime_saves_what_needs_no_core(tmp_path, monkeypatch):
    settings_file(tmp_path, '[modules]\nmemory = "gone"\nfrontend = "scripted"\n')

    async def script(runtime: Any, frontend: ScriptedFrontend) -> None:
        events = runtime.events()
        assert runtime.snapshot().conversation is None
        with pytest.raises(ValueError, match="once Kayden has started"):
            await runtime.update_settings(SettingsUpdate(model="fake-1"))
        for call in (runtime.list_conversations(), runtime.list_models(), runtime.memories(),
                     runtime.send("hi")):
            with pytest.raises(RuntimeStateError, match="'gone' is not installed"):
                await call
        s = await runtime.update_settings(SettingsUpdate(
            modules={"memory": "fake-memory"}, system_prompt="Be brief."))
        assert s.system_prompt == "Be brief." and s.modules["memory"].chosen == "fake-memory"
        envelope = await anext(events)
        assert isinstance(envelope.event, SettingsChanged)

    scripted(monkeypatch, script)
    await asyncio.wait_for(launcher.launch(args(tmp_path), TERMINAL), 10)
    saved = (tmp_path / "settings.toml").read_text()
    assert 'memory = "fake-memory"' in saved and 'system_prompt = "Be brief."' in saved


@pytest.mark.parametrize(("config", "message"), [
    ("", "kayden.core.anthropic:factory: ANTHROPIC_API_KEY is not set"),
    ("[core]\nfactory = 'kayden.core.openai_compat:factory'\noptions = {profile = 'deepseek'}\n",
     "kayden.core.openai_compat:factory: set DEEPSEEK_API_KEY or the api_key option"),
    ("[core]\nfactory = 'kayden.core.anthropic:nothing'\n",
     "kayden.core.anthropic:nothing: module 'kayden.core.anthropic' has no attribute 'nothing'"),
    ("[core]\noptions = {api_key = 'x', colour = 'red'}\n",
     "kayden.core.anthropic:factory: AnthropicCore.__init__() got an unexpected keyword"),
    ("[core]\nfactory = 'kayden.core.nowhere:factory'\n",
     "kayden.core.nowhere:factory: No module named 'kayden.core.nowhere'"),
    ("[core]\nfactory = 'tests.fakes.core:factory'\n[memory]\nfactory = 'tests.fakes.core:factory'\n",
     "tests.fakes.core:factory returned ScriptedCore, not Memory"),
])
async def test_bad_module_options_open_the_frontend_with_the_error(tmp_path, monkeypatch,
                                                                   config, message):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.toml").write_text(config)
    settings_file(tmp_path, '[modules]\nfrontend = "scripted"\n')
    seen = []

    async def script(runtime: Any, frontend: ScriptedFrontend) -> None:
        seen.append((await runtime.settings()).startup_error)

    scripted(monkeypatch, script)
    await asyncio.wait_for(launcher.launch(args(tmp_path), TERMINAL), 10)
    assert seen[0].startswith(message) and "\n" not in seen[0]


async def test_a_frontend_that_cannot_be_built_falls_back_to_the_launchers(tmp_path,
                                                                          monkeypatch):
    (tmp_path / "config.toml").write_text("[frontend]\nfactory = 'tests.fakes.nowhere:x'\n")
    config = launcher.load_config(args(tmp_path))
    seen = []

    async def script(runtime: Any, frontend: ScriptedFrontend) -> None:
        s = await runtime.settings()
        seen.append((s.startup_error, s.modules["frontend"].running))

    scripted(monkeypatch, script)
    await asyncio.wait_for(run_instance(config, fallback="scripted"), 10)
    assert seen == [("tests.fakes.nowhere:x: No module named 'tests.fakes.nowhere'",
                     "scripted")]


# ── From the terminal to the desktop app ──


async def test_restart_reopens_the_conversation_with_the_new_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRST_API_KEY", "k")
    settings_file(tmp_path, FAKE_MODULES + 'frontend = "scripted"\n')
    seen: dict[str, Any] = {}

    async def first_run(runtime: Any, frontend: ScriptedFrontend) -> None:
        seen["conversation"] = (await runtime.start(StartOptions())).id
        await runtime.update_settings(SettingsUpdate(modules={"core": "second"}))
        await runtime.restart()

    async def second_run(runtime: Any, frontend: ScriptedFrontend) -> None:
        seen["options"] = frontend.options
        seen["models"] = [m.id for m in await runtime.list_models()]

    scripted(monkeypatch, first_run, second_run)
    await asyncio.wait_for(launcher.launch(args(tmp_path), TERMINAL), 10)
    assert seen["options"] == {"start": True, "conversation_id": seen["conversation"]}
    assert seen["models"] == ["s-1", "s-2"]


def test_the_desktop_app_starts_after_the_terminal_let_go_of_the_data(tmp_path, monkeypatch):
    """The TUI exits on ShuttingDown, while Restart now is still shutting down; the hand
    over waits until the data directory is free."""
    with_display(monkeypatch, True)
    monkeypatch.setenv("FIRST_API_KEY", "k")
    settings_file(tmp_path, FAKE_MODULES + 'frontend = "scripted"\n')

    async def slow_close(self: Any) -> None:  # SQLite and HTTP clients take a moment
        await asyncio.sleep(0.2)

    monkeypatch.setattr(InMemoryMemory, "aclose", slow_close, raising=False)

    async def script(runtime: Any, frontend: ScriptedFrontend) -> None:
        recorder = Recorder(runtime)
        await runtime.start(StartOptions())
        await runtime.update_settings(SettingsUpdate(modules={"frontend": "desktop"}))
        asyncio.get_running_loop().create_task(runtime.restart())
        await recorder.wait_for(lambda r: r.of(ShuttingDown))

    free = []

    def spawn(command: Any, **kw: Any) -> None:
        InstanceLock.acquire(tmp_path).release()
        free.append(True)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    scripted(monkeypatch, script)
    launcher.main(["--data-dir", str(tmp_path)])
    assert free == [True]


async def test_a_restart_in_the_same_frontend_waits_for_the_data(tmp_path, monkeypatch):
    """Any module change: the TUI exits on ShuttingDown, and the next instance in the same
    process still gets the data directory."""
    monkeypatch.setenv("FIRST_API_KEY", "k")
    settings_file(tmp_path, FAKE_MODULES + 'frontend = "scripted"\n')

    async def slow_close(self: Any) -> None:
        await asyncio.sleep(0.2)

    monkeypatch.setattr(InMemoryMemory, "aclose", slow_close, raising=False)
    seen = []

    async def first_run(runtime: Any, frontend: ScriptedFrontend) -> None:
        recorder = Recorder(runtime)
        await runtime.start(StartOptions())
        await runtime.update_settings(SettingsUpdate(modules={"core": "second"}))
        asyncio.get_running_loop().create_task(runtime.restart())
        await recorder.wait_for(lambda r: r.of(ShuttingDown))

    async def second_run(runtime: Any, frontend: ScriptedFrontend) -> None:
        seen.append((await runtime.settings()).startup_error)

    scripted(monkeypatch, first_run, second_run)
    await asyncio.wait_for(launcher.launch(args(tmp_path), TERMINAL), 10)
    assert seen == [None]


def test_the_terminal_hands_over_to_the_desktop_app(tmp_path, monkeypatch, capsys):
    """Choosing a window frontend in a terminal frontend and pressing Restart now quits
    and opens the desktop app on the same data and conversation."""
    with_display(monkeypatch, True)
    monkeypatch.setenv("FIRST_API_KEY", "k")
    settings_file(tmp_path, FAKE_MODULES + 'frontend = "scripted"\n')
    seen: dict[str, Any] = {}

    async def script(runtime: Any, frontend: ScriptedFrontend) -> None:
        seen["conversation"] = (await runtime.start(StartOptions())).id
        s = await runtime.update_settings(SettingsUpdate(modules={"frontend": "desktop"}))
        assert s.restart_frontend == "desktop"
        await runtime.restart()

    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kw: spawned.append((command, kw)))
    scripted(monkeypatch, script)
    launcher.main(["--data-dir", str(tmp_path)])
    [(command, kw)] = spawned
    assert command == [sys.executable, "-m", "kayden.frontends.desktop.launcher",
                       "--data-dir", str(tmp_path), "--conversation", seen["conversation"]]
    assert kw["start_new_session"] and kw["stdin"] == subprocess.DEVNULL
    assert "continues in the desktop app" in capsys.readouterr().out
    # The desktop launcher then runs the desktop app on that conversation.
    desktop_args = desktop_launcher.parse_args(command[3:])
    config = launcher.load_config(desktop_args, launcher=WINDOW)
    assert config.frontend == ModuleSpec("kayden.frontends.desktop:factory",
                                         {"start": True, "conversation_id": seen["conversation"]})
    assert config.runtime.data_dir == tmp_path


def test_the_handed_over_command_runs(tmp_path):
    """The command the terminal launches is a working desktop launcher."""
    command = launcher.window_command(args(tmp_path), Restart("desktop", WINDOW, None))
    out = subprocess.run([*command[:3], "--help"], capture_output=True, text=True, check=True,
                         timeout=60)
    assert out.stdout.startswith("usage: python -m kayden.frontends.desktop.launcher ")


async def test_a_data_folder_in_use_opens_the_frontend_with_the_error(tmp_path, monkeypatch):
    """A second Kayden on the same data folder shows why it cannot start, and touches no
    stored data."""
    from kayden.runtime.instance import InstanceLock

    monkeypatch.setenv("FIRST_API_KEY", "k")
    settings_file(tmp_path, FAKE_MODULES + 'frontend = "scripted"\n')
    seen = []

    async def script(runtime: Any, frontend: ScriptedFrontend) -> None:
        seen.append((await runtime.settings()).startup_error)
        with pytest.raises(RuntimeStateError):
            await runtime.list_conversations()

    held = InstanceLock.acquire(tmp_path)
    try:
        scripted(monkeypatch, script)
        await asyncio.wait_for(launcher.launch(args(tmp_path), TERMINAL), 10)
    finally:
        held.release()
    assert seen[0].startswith("Kayden is already open in another window")


# ── Launching kayden with a window frontend saved ──


def test_kayden_opens_the_saved_window_frontend(tmp_path, monkeypatch, capsys):
    """Choosing the desktop app means `kayden` opens it, without starting the TUI."""
    with_display(monkeypatch, True)
    settings_file(tmp_path, FAKE_MODULES + 'frontend = "desktop"\n')
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kw: spawned.append(command))
    monkeypatch.setenv("FIRST_API_KEY", "k")
    monkeypatch.setattr(launcher, "LAUNCHER_FRONTENDS", {**launcher.LAUNCHER_FRONTENDS,
                                                         TERMINAL: "scripted"})
    ran = []

    async def script(runtime: Any, frontend: ScriptedFrontend) -> None:
        ran.append(True)

    scripted(monkeypatch, script)
    launcher.main(["--data-dir", str(tmp_path), "--conversation", "c1"])
    assert not ran
    assert spawned == [[sys.executable, "-m", "kayden.frontends.desktop.launcher",
                        "--data-dir", str(tmp_path), "--conversation", "c1"]]
    assert capsys.readouterr().out == "Kayden opens in the desktop app.\n"


@pytest.mark.parametrize(("saved", "display", "notice"), [
    ("desktop", False, ("No display here, so Kayden opens in the terminal; Desktop app "
                       "opens when you start Kayden from the app menu.")),
    ("gone", True, ("The interface 'gone' is not installed, so Kayden opens in the "
                   "terminal.")),
])
def test_kayden_says_why_it_opens_in_the_terminal(tmp_path, monkeypatch, capsys, saved,
                                                  display, notice):
    with_display(monkeypatch, display)
    monkeypatch.setenv("FIRST_API_KEY", "k")
    settings_file(tmp_path, FAKE_MODULES + f'frontend = "{saved}"\n')
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kw: spawned.append(command))
    ran = []

    async def script(runtime: Any, frontend: ScriptedFrontend) -> None:
        ran.append(True)

    monkeypatch.setattr(launcher, "LAUNCHER_FRONTENDS", {**launcher.LAUNCHER_FRONTENDS,
                                                         TERMINAL: "scripted"})
    scripted(monkeypatch, script)
    launcher.main(["--data-dir", str(tmp_path)])
    assert not spawned and ran
    assert capsys.readouterr().out == notice + "\n"
