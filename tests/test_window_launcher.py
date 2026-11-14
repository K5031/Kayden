"""Each frontend runs on the event loop it names; no launcher assumes a toolkit."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import kadence.__main__ as launcher
from kadence import window
from kadence.api import SettingsUpdate, StartOptions
from kadence.runtime import ModuleSpec
from tests.fakes import frontend as fake_frontend
from tests.fakes.frontend import ScriptedFrontend
from tests.test_modules import FAKE_MODULES, settings_file


@pytest.fixture(autouse=True)
def _modules(installed, clean_env, monkeypatch):
    monkeypatch.setenv("FIRST_API_KEY", "k")


def loop(name: str, used: list[str]) -> Any:
    def run(main: Any) -> Any:
        used.append(name)
        return asyncio.run(main)

    return run


def window_frontends(monkeypatch: pytest.MonkeyPatch, *scripts: Any, event_loop: Any = None,
                     other_loop: Any = None) -> None:
    """scripted-window runs `scripts` in turn on `event_loop`; scripted-window-2 too, on
    `other_loop`."""
    queue = list(scripts)

    def build(options: dict[str, Any], paths: Any) -> ScriptedFrontend:
        return ScriptedFrontend(queue.pop(0), kind="window", options=options)

    for name, run in (("window_factory", event_loop), ("other_window_factory", other_loop)):
        def factory(options: dict[str, Any], paths: Any) -> ScriptedFrontend:
            return build(options, paths)

        factory.kind = "window"  # type: ignore[attr-defined]
        if run is not None:
            factory.event_loop = run  # type: ignore[attr-defined]
        monkeypatch.setattr(fake_frontend, name, factory)


def test_a_frontend_without_a_loop_of_its_own_runs_on_asyncio():
    assert launcher.frontend_loop(ModuleSpec("kadence.frontends.tui:factory")) is asyncio.run


def test_the_desktop_app_names_the_qt_loop():
    from kadence.frontends.desktop import factory
    from kadence.frontends.desktop.loop import run_on_qt

    assert launcher.frontend_loop(ModuleSpec("kadence.frontends.desktop:factory")) is run_on_qt
    assert factory.event_loop is run_on_qt


def test_the_window_launcher_runs_the_chosen_frontend_on_its_loop(tmp_path, monkeypatch):
    settings_file(tmp_path, FAKE_MODULES + 'frontend = "scripted-window"\n')
    used: list[str] = []
    ran = []

    async def script(runtime: Any, frontend: ScriptedFrontend) -> None:
        ran.append(frontend.info.kind)

    window_frontends(monkeypatch, script, event_loop=loop("first", used))
    window.main(["--data-dir", str(tmp_path)])
    assert ran == ["window"] and used == ["first"]


def test_a_restart_to_a_frontend_on_another_loop_changes_loops(tmp_path, monkeypatch):
    settings_file(tmp_path, FAKE_MODULES + 'frontend = "scripted-window"\n')
    used: list[str] = []
    seen = []

    async def first(runtime: Any, frontend: ScriptedFrontend) -> None:
        seen.append((await runtime.start(StartOptions())).id)
        await runtime.update_settings(SettingsUpdate(modules={"frontend": "scripted-window-2"}))
        await runtime.restart()

    async def second(runtime: Any, frontend: ScriptedFrontend) -> None:
        seen.append(frontend.options.get("conversation_id"))

    window_frontends(monkeypatch, first, second, event_loop=loop("first", used),
                     other_loop=loop("second", used))
    window.main(["--data-dir", str(tmp_path)])
    assert used == ["first", "second"]
    assert seen[1] == seen[0]  # the same conversation, reopened on the other loop


def test_a_bad_config_is_shown_by_the_launchers_own_frontend(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text("not = [valid")
    shown: list[str] = []

    def run(main: Any) -> Any:
        try:
            return asyncio.run(main)
        except SystemExit as e:
            shown.append(str(e.code))
            raise

    from kadence.frontends import desktop

    monkeypatch.setattr(desktop.factory, "event_loop", run)
    with pytest.raises(SystemExit):
        window.main(["--data-dir", str(tmp_path)])
    assert shown and "invalid TOML" in shown[0]


def test_the_window_launcher_has_only_its_own_flags(tmp_path):
    """What the terminal launcher's flags set is left at its default, not copied here."""
    assert window.parse_args(["--data-dir", str(tmp_path), "--conversation", "c1"]) == (
        launcher.Launch(tmp_path, conversation="c1"))
    assert launcher.parse_args(["--data-dir", str(tmp_path), "--model", "m"]) == (
        launcher.Launch(tmp_path, model="m"))
    with pytest.raises(SystemExit):
        window.parse_args(["--model", "m"])


def test_the_window_makes_no_folder_of_its_own(tmp_path, monkeypatch):
    """Its tools start where it is started, as the terminal app's do; each chat is then
    given a folder."""
    monkeypatch.chdir(tmp_path)
    assert not hasattr(window, "DEFAULT_WORK_FOLDER")
    settings_file(tmp_path, '[modules]\ncore = "first"\nmemory = "fake-memory"\n'
                  'frontend = "scripted-window"\n')
    roots: list[tuple[str, bool]] = []
    loads: list[Path] = []
    load = launcher.load_config
    monkeypatch.setattr(launcher, "load_config",
                        lambda args, **kw: (loads.append(args.data_dir), load(args, **kw))[1])

    async def script(runtime: Any, frontend: ScriptedFrontend) -> None:
        root = (await runtime.settings()).work_folder
        roots.append((root, Path(root).is_dir()))

    window_frontends(monkeypatch, script)
    window.main(["--data-dir", str(tmp_path)])
    assert roots == [(str(tmp_path), True)]
    assert len(loads) == 2  # to choose the loop, then to run on it: never by the launcher itself
