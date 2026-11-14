"""User settings: settings() and update_settings() (api.py RuntimeAPI). Runtime settings
apply live; module settings are saved for the next start (see also test_modules.py)."""

from __future__ import annotations

import json
import os
import tomllib
from typing import Any

import pytest

from kadence.api import (
    PERMISSION_CEILING_ENV,
    AgentState,
    Budget,
    ModelSelected,
    PermissionPolicy,
    RuntimeStateError,
    SettingsChanged,
    SettingsUpdate,
    ToolStatus,
)
from kadence.runtime import ModuleState
from kadence.runtime.config import DEFAULT_SYSTEM_PROMPT
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools

RUNNING = {"core": "first", "memory": "fake-memory", "tools": "fake-tools",
           "frontend": "scripted"}


@pytest.fixture(autouse=True)
def _modules(installed):
    """The fake modules are installed for every test here."""


async def setup(make: Any, **config: Any) -> Harness:
    state = ModuleState(running=RUNNING, work_folder="/start")
    return await make(**{"modules": state, **config})


def saved(h: Harness) -> dict[str, Any]:
    return tomllib.loads((h.data_dir / "settings.toml").read_text())


def logged_settings(h: Harness) -> list[dict[str, Any]]:
    lines = (h.data_dir / "events.jsonl").read_text().splitlines()
    return [r for r in map(json.loads, lines) if r.get("decision") == "settings"]


# ── Reading ──


async def test_settings_reflect_the_configuration(make, monkeypatch):
    monkeypatch.setenv("FIRST_API_KEY", "from-env")
    h = await setup(make)
    s = await h.runtime.settings()
    core = s.modules["core"]
    assert (core.running, core.chosen) == ("first", "first")
    infos = {m.id: m for m in core.installed}
    assert (infos["first"].name, infos["first"].key_name, infos["first"].has_key) == (
        "First AI", "FIRST_API_KEY", True)
    assert not infos["second"].has_key and "broken" not in infos  # its package is missing
    assert {"anthropic", "deepseek", "openai"} <= set(infos)
    assert s.model is None and s.maintenance_model is None
    assert s.permissions is PermissionPolicy.ALLOW_READ_ONLY
    assert s.system_prompt == DEFAULT_SYSTEM_PROMPT
    assert s.budget == Budget()
    assert s.work_folder == "/start"
    assert not s.restart_needed and s.startup_error is None
    assert not s.already_open
    assert s.restart_frontend == "scripted"


async def test_settings_without_module_ids(make):
    h = await make()
    s = await h.runtime.settings()
    assert s.modules["core"].running is None and s.work_folder is None
    assert not s.restart_needed


# ── Live changes ──


async def test_live_settings_apply_from_the_next_request(make):
    h = await setup(make)
    await h.start()
    s = await h.runtime.update_settings(SettingsUpdate(
        model="fake-2", maintenance_model="fake-1", system_prompt="Be brief.",
        budget=Budget(max_tokens=1_000_000), permissions=PermissionPolicy.DENY))
    assert (s.model, s.maintenance_model, s.system_prompt) == ("fake-2", "fake-1", "Be brief.")
    assert s.budget == Budget(max_tokens=1_000_000) and s.permissions is PermissionPolicy.DENY
    assert h.runtime.snapshot().model == "fake-2"
    [changed] = await h.rec.wait_for(lambda r: r.of(SettingsChanged))
    assert changed.settings == s
    assert h.rec.of(ModelSelected)[-1] == ModelSelected("fake-2")
    config = h.runtime.snapshot().config
    assert config.budget == s.budget and config.permissions is PermissionPolicy.DENY

    h.tools.instant("write")
    h.core.add(Step(calls=[call("write", id="w")]), Step(text="done"))
    await h.turn("go")
    request = h.core.requests[-2]
    assert request.model == "fake-2" and request.system.startswith("Be brief.")
    assert (await h.results())["w"].status == ToolStatus.REFUSED  # DENY applied


async def test_maintenance_model_empty_string_clears_it(make):
    h = await setup(make, maintenance_model="fake-2")
    await h.start()
    s = await h.runtime.update_settings(SettingsUpdate(maintenance_model=""))
    assert s.maintenance_model is None


async def test_permissions_stay_clamped_to_the_inherited_ceiling(make, monkeypatch):
    monkeypatch.setenv(PERMISSION_CEILING_ENV, "deny")
    h = await setup(make, permissions=PermissionPolicy.DENY)
    s = await h.runtime.update_settings(
        SettingsUpdate(permissions=PermissionPolicy.ALLOW_READ_ONLY))
    assert s.permissions is PermissionPolicy.DENY


async def test_permissions_loosen_past_the_policy_this_runtime_exported(make):
    h = await setup(make, permissions=PermissionPolicy.DENY)
    s = await h.runtime.update_settings(SettingsUpdate(permissions=PermissionPolicy.ASK))
    assert s.permissions is PermissionPolicy.ASK
    assert os.environ[PERMISSION_CEILING_ENV] == "ask"


async def test_raising_a_spent_budget_lets_the_agent_continue(make):
    h = await setup(make, budget=Budget(max_tool_calls=0))
    await h.start()
    await h.runtime.send("blocked")
    await h.rec.wait_state(AgentState.FAILED)
    with pytest.raises(RuntimeStateError):
        await h.runtime.send("still blocked")
    await h.runtime.update_settings(SettingsUpdate(budget=Budget(max_tool_calls=10)))
    await h.turn("go")
    assert h.core.requests


@pytest.mark.parametrize("update", [
    SettingsUpdate(model="nope"),
    SettingsUpdate(maintenance_model="nope"),
    SettingsUpdate(system_prompt="   "),
    SettingsUpdate(modules={"core": "unknown"}),
    SettingsUpdate(modules={"core": "broken"}),
    SettingsUpdate(modules={"engine": "first"}),
    SettingsUpdate(budget=Budget(max_cost_usd=1.0), model="fake-2"),  # fake-2 has no prices
    SettingsUpdate(work_folder="/does/not/exist"),
    SettingsUpdate(api_key="   "),
])
async def test_an_invalid_update_changes_nothing(make, update, keychain):
    h = await setup(make)
    await h.start()
    before = await h.runtime.settings()
    with pytest.raises(ValueError):
        await h.runtime.update_settings(update)
    assert await h.runtime.settings() == before
    assert not (h.data_dir / "settings.toml").exists()
    assert not keychain.store
    assert not h.rec.of(SettingsChanged)


# ── Module settings: saved for the next start ──


async def test_choosing_a_module_saves_it_and_builds_nothing(make):
    h = await setup(make)
    await h.start()
    await h.turn("hello")
    s = await h.runtime.update_settings(SettingsUpdate(modules={"core": "second"}))
    assert (s.modules["core"].running, s.modules["core"].chosen) == ("first", "second")
    assert s.restart_needed
    assert saved(h) == {"modules": {"core": "second"}, "model": "", "maintenance_model": ""}
    assert [m.id for m in await h.runtime.list_models()] == ["fake-1", "fake-2"]
    await h.turn("again")
    assert len(h.core.requests) == 2 and not h.core.closed  # still the running Core
    [changed] = await h.rec.wait_for(lambda r: r.of(SettingsChanged))
    assert changed.settings == s


async def test_a_model_chosen_while_another_core_waits_is_not_saved(make):
    h = await setup(make)
    await h.start()
    s = await h.runtime.update_settings(SettingsUpdate(modules={"core": "second"},
                                                       model="fake-2"))
    assert s.model == "fake-2"  # the running Core's, from the next request
    await h.runtime.update_settings(SettingsUpdate(maintenance_model="fake-1"))
    assert saved(h) == {"modules": {"core": "second"}, "model": "", "maintenance_model": ""}


async def test_choosing_the_running_module_again_needs_no_restart(make):
    h = await setup(make)
    await h.runtime.update_settings(SettingsUpdate(modules={"core": "second"}))
    s = await h.runtime.update_settings(SettingsUpdate(modules={"core": "first"}))
    assert not s.restart_needed
    assert saved(h)["modules"] == {"core": "first"}


async def test_the_key_applies_at_the_next_start_the_folder_at_the_next_chat(
        make, tmp_path, keychain):
    h = await setup(make)
    await h.start()
    s = await h.runtime.update_settings(SettingsUpdate(api_key="sk-1",
                                                       work_folder=str(tmp_path)))
    assert keychain.store[("kadence", "FIRST_API_KEY")] == "sk-1"
    assert s.work_folder == str(tmp_path) and s.restart_needed
    assert h.runtime._tools == [h.tools] and not h.tools.closed  # nothing was rebuilt
    assert h.runtime._core is h.core and not h.core.closed
    assert saved(h) == {"work_folder": str(tmp_path)}


async def test_module_settings_save_while_the_agent_works(make, tmp_path):
    tools = FakeTools()
    tools.hanging("bg")
    core = ScriptedCore([Step(calls=[call("bg", id="bg")]), Step(text="started")])
    h = await setup(make, core=core, tools=tools)
    await h.start()
    await h.turn("go")  # bg is backgrounded and still running
    s = await h.runtime.update_settings(SettingsUpdate(
        modules={"core": "second"}, work_folder=str(tmp_path), api_key="k"))
    assert s.restart_needed
    assert h.runtime.snapshot().operations  # still running


async def test_module_settings_before_start(make):
    h = await setup(make)
    s = await h.runtime.update_settings(SettingsUpdate(modules={"core": "second"}))
    assert s.modules["core"].chosen == "second"
    await h.start()
    assert h.runtime.snapshot().model == "fake-1"  # the running Core's


async def test_api_key_goes_to_the_keychain_only(make, keychain):
    h = await setup(make)
    await h.start()
    s = await h.runtime.update_settings(SettingsUpdate(modules={"core": "second"},
                                                       api_key="sk-2"))
    assert keychain.store[("kadence", "SECOND_API_KEY")] == "sk-2"
    assert [m.has_key for m in s.modules["core"].installed if m.id == "second"] == [True]
    assert "sk-2" not in (h.data_dir / "settings.toml").read_text()
    assert "sk-2" not in (h.data_dir / "events.jsonl").read_text()


async def test_api_key_is_for_the_chosen_core(make, keychain):
    h = await setup(make)
    await h.runtime.update_settings(SettingsUpdate(modules={"core": "second"}))
    await h.runtime.update_settings(SettingsUpdate(api_key="sk-2"))
    assert keychain.store == {("kadence", "SECOND_API_KEY"): "sk-2"}


async def test_api_key_for_a_core_without_keys_is_refused(make):
    h = await setup(make)
    with pytest.raises(ValueError, match="takes no API key"):
        await h.runtime.update_settings(SettingsUpdate(modules={"core": "keyless"},
                                                       api_key="x"))


async def test_a_failed_save_restores_the_keychain(make, keychain, monkeypatch):
    keychain.store[("kadence", "FIRST_API_KEY")] = "old"
    h = await setup(make)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("kadence.runtime.modules.save_settings", fail)
    with pytest.raises(OSError):
        await h.runtime.update_settings(SettingsUpdate(api_key="new", system_prompt="Hi."))
    assert keychain.store[("kadence", "FIRST_API_KEY")] == "old"
    s = await h.runtime.settings()
    assert not s.restart_needed and s.system_prompt == DEFAULT_SYSTEM_PROMPT


# ── Saving and logging ──


async def test_only_changed_settings_are_saved(make, tmp_path):
    h = await setup(make)
    await h.start()
    await h.runtime.update_settings(SettingsUpdate(system_prompt="Be brief."))
    assert saved(h) == {"system_prompt": "Be brief."}
    await h.runtime.update_settings(SettingsUpdate(
        modules={"frontend": "scripted-window"}, budget=Budget(max_cost_usd=5.0),
        permissions=PermissionPolicy.ASK, work_folder=str(tmp_path)))
    await h.runtime.update_settings(SettingsUpdate(modules={"memory": "sqlite"}))
    assert saved(h) == {"system_prompt": "Be brief.", "permissions": "ask",
                        "work_folder": str(tmp_path), "budget": {"max_cost_usd": 5.0},
                        "modules": {"frontend": "scripted-window", "memory": "sqlite"}}


async def test_settings_changes_are_logged_without_values_that_are_secret(make):
    h = await setup(make)
    await h.start()
    await h.runtime.update_settings(SettingsUpdate(modules={"core": "second"}, api_key="sk-2",
                                                   permissions=PermissionPolicy.DENY))
    [record] = logged_settings(h)
    assert record["changed"] == ["modules", "api_key", "permissions"]
    assert record["modules"] == {"core": "second"} and record["permissions"] == "deny"
    assert "api_key" not in {k for k in record if k != "changed"}


async def test_raising_a_limit_and_loosening_permissions_are_flagged(make):
    h = await setup(make, permissions=PermissionPolicy.DENY, budget=Budget(max_tool_calls=5))
    await h.start()
    await h.runtime.update_settings(SettingsUpdate(
        permissions=PermissionPolicy.ASK, budget=Budget(max_tool_calls=50)))
    [record] = logged_settings(h)
    assert record["loosened"] == ["budget", "permissions"]
    await eventually(lambda: h.rec.of(SettingsChanged))


async def test_settings_before_start_need_the_provider_only_for_models(make):
    h = await setup(make)
    h.core.list_error = RuntimeError("no key yet")
    s = await h.runtime.update_settings(SettingsUpdate(system_prompt="Be brief."))
    assert s.system_prompt == "Be brief."
