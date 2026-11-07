"""User settings: settings() and update_settings() (api.py RuntimeAPI)."""

from __future__ import annotations

import json
import tomllib
from typing import Any

import pytest

from kayden.api import (
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
from kayden.runtime import ModuleSpec, Provider
from kayden.runtime.config import DEFAULT_SYSTEM_PROMPT
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools

FAKE_CORE = "tests.fakes.core:factory"
PROVIDERS = (
    Provider("first", "First AI", ModuleSpec(FAKE_CORE), key_name="FIRST_API_KEY"),
    Provider("second", "Second AI", ModuleSpec(FAKE_CORE, {"models": ["s-1", "s-2"]}),
             key_name="SECOND_API_KEY"),
    Provider("broken", "Broken AI", ModuleSpec(FAKE_CORE, {"list_error": "invalid key"})),
)
TOOLS = ModuleSpec("tests.fakes.tools:factory", {"root": "/start"})


async def setup(make: Any, **config: Any) -> Harness:
    config = {"providers": PROVIDERS, "provider": "first", "tools_module": TOOLS, **config}
    return await make(**config)


def saved(h: Harness) -> dict[str, Any]:
    return tomllib.loads((h.data_dir / "settings.toml").read_text())


def logged_settings(h: Harness) -> list[dict[str, Any]]:
    lines = (h.data_dir / "events-session.jsonl").read_text().splitlines()
    return [r for r in map(json.loads, lines) if r.get("decision") == "settings"]


# ── Reading ──


async def test_settings_reflect_the_configuration(make, monkeypatch):
    monkeypatch.setenv("FIRST_API_KEY", "from-env")
    h = await setup(make)
    s = await h.runtime.settings()
    assert s.provider == "first"
    assert [(p.id, p.name, p.has_key) for p in s.providers] == [
        ("first", "First AI", True), ("second", "Second AI", False),
        ("broken", "Broken AI", False)]
    assert s.model is None and s.maintenance_model is None
    assert s.permissions is PermissionPolicy.ALLOW_READ_ONLY
    assert s.system_prompt == DEFAULT_SYSTEM_PROMPT
    assert s.budget == Budget()
    assert s.work_folder == "/start"


async def test_settings_without_a_provider_list(make):
    h = await make()
    s = await h.runtime.settings()
    assert s.provider is None and s.providers == [] and s.work_folder is None


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
    assert request.model == "fake-2" and request.system == "Be brief."
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
    SettingsUpdate(provider="unknown"),
    SettingsUpdate(budget=Budget(max_cost_usd=1.0), model="fake-2"),  # fake-2 has no prices
    SettingsUpdate(work_folder="/does/not/exist"),
])
async def test_an_invalid_update_changes_nothing(make, update):
    h = await setup(make)
    await h.start()
    before = await h.runtime.settings()
    with pytest.raises(ValueError):
        await h.runtime.update_settings(update)
    assert await h.runtime.settings() == before
    assert not (h.data_dir / "settings.toml").exists()
    assert not h.rec.of(SettingsChanged)


# ── Provider and work folder ──


async def test_switching_provider_rebuilds_the_core(make):
    h = await setup(make, maintenance_model="fake-2")
    await h.start()
    await h.turn("hello")
    old = h.core
    s = await h.runtime.update_settings(SettingsUpdate(provider="second"))
    assert (s.provider, s.model, s.maintenance_model) == ("second", "s-1", None)
    assert old.closed
    assert [m.id for m in await h.runtime.list_models()] == ["s-1", "s-2"]
    await h.turn("again")
    assert len(old.requests) == 1  # the next turn went to the new Core
    assert h.runtime.snapshot().state == AgentState.IDLE


async def test_switching_provider_can_pick_the_model(make):
    h = await setup(make)
    await h.start()
    s = await h.runtime.update_settings(SettingsUpdate(provider="second", model="s-2"))
    assert s.model == "s-2"


async def test_a_provider_that_cannot_list_models_is_not_switched_to(make):
    h = await setup(make)
    await h.start()
    with pytest.raises(ValueError, match="invalid key"):
        await h.runtime.update_settings(SettingsUpdate(provider="broken"))
    assert (await h.runtime.settings()).provider == "first"
    assert not h.core.closed


async def test_provider_and_work_folder_wait_until_nothing_runs(make):
    tools = FakeTools()
    tools.hanging("bg")
    core = ScriptedCore([Step(calls=[call("bg", id="bg")]), Step(text="started")])
    h = await setup(make, core=core, tools=tools)
    await h.start()
    await h.turn("go")  # bg is backgrounded and still running
    for update in (SettingsUpdate(provider="second"), SettingsUpdate(work_folder="/tmp"),
                   SettingsUpdate(api_key="k")):
        with pytest.raises(RuntimeStateError):
            await h.runtime.update_settings(update)
    await h.runtime.update_settings(SettingsUpdate(model="fake-2"))  # live settings still apply


async def test_work_folder_rebuilds_the_tools(make, tmp_path):
    h = await setup(make)
    await h.start()
    s = await h.runtime.update_settings(SettingsUpdate(work_folder=str(tmp_path)))
    assert s.work_folder == str(tmp_path)
    assert h.tools.closed
    tools = h.runtime._tools  # the runtime's current Tools module
    assert isinstance(tools, FakeTools) and tools.options == {"root": str(tmp_path)}


async def test_settings_before_start(make):
    h = await setup(make)
    s = await h.runtime.update_settings(SettingsUpdate(provider="second"))
    assert s.provider == "second"
    info = await h.start()
    assert info and h.runtime.snapshot().model == "s-1"


# ── Keys ──


async def test_api_key_goes_to_the_keychain_only(make, keychain):
    h = await setup(make)
    await h.start()
    s = await h.runtime.update_settings(SettingsUpdate(provider="second", api_key="sk-2"))
    assert keychain.store[("kayden", "SECOND_API_KEY")] == "sk-2"
    assert [p.has_key for p in s.providers if p.id == "second"] == [True]
    assert "sk-2" not in (h.data_dir / "settings.toml").read_text()
    assert "sk-2" not in (h.data_dir / "events-session.jsonl").read_text()


async def test_api_key_without_provider_is_for_the_current_one(make, keychain):
    h = await setup(make)
    await h.runtime.update_settings(SettingsUpdate(api_key="sk-1"))
    assert keychain.store[("kayden", "FIRST_API_KEY")] == "sk-1"


async def test_api_key_for_a_provider_without_keys_is_refused(make):
    h = await setup(make)
    with pytest.raises(ValueError):
        await h.runtime.update_settings(SettingsUpdate(provider="broken", api_key="x"))


# ── Saving and logging ──


async def test_only_changed_settings_are_saved(make, tmp_path):
    h = await setup(make)
    await h.start()
    await h.runtime.update_settings(SettingsUpdate(system_prompt="Be brief."))
    assert saved(h) == {"system_prompt": "Be brief."}
    await h.runtime.update_settings(SettingsUpdate(
        provider="second", budget=Budget(max_cost_usd=5.0),
        permissions=PermissionPolicy.ASK, work_folder=str(tmp_path)))
    assert saved(h) == {"system_prompt": "Be brief.", "provider": "second", "model": "s-1",
                        "permissions": "ask", "work_folder": str(tmp_path),
                        "budget": {"max_cost_usd": 5.0}}


async def test_settings_changes_are_logged_without_values_that_are_secret(make):
    h = await setup(make)
    await h.start()
    await h.runtime.update_settings(SettingsUpdate(api_key="sk-1", model="fake-2"))
    [record] = logged_settings(h)
    assert record["changed"] == ["api_key", "model"] and record["model"] == "fake-2"
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
