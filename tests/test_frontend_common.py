"""Settings texts and the loosening check shared by the TUI and the desktop app."""

from kayden.api import Budget, ModelInfo, PermissionPolicy, Settings, SettingsUpdate
from kayden.frontends.common import dollars, loosening, model_label


def _settings(limit: float | None = 10.0,
              permissions: PermissionPolicy = PermissionPolicy.ASK) -> Settings:
    return Settings(provider="anthropic", providers=(), model="m", maintenance_model=None,
                    budget=Budget(max_cost_usd=limit), permissions=permissions,
                    system_prompt="p", work_folder="/w")


def test_tightening_and_unrelated_changes_need_no_confirmation():
    current = _settings()
    assert loosening(current, SettingsUpdate(budget=Budget(max_cost_usd=5.0))) == []
    assert loosening(current, SettingsUpdate(permissions=PermissionPolicy.DENY)) == []
    assert loosening(current, SettingsUpdate(model="other", system_prompt="q")) == []
    assert loosening(_settings(limit=None), SettingsUpdate(budget=Budget(max_cost_usd=3.0))) == []


def test_raising_removing_and_loosening_are_each_named():
    current = _settings()
    assert loosening(current, SettingsUpdate(budget=Budget(max_cost_usd=50.0))) == [
        "raises the spending limit from $10 to $50"]
    assert loosening(current, SettingsUpdate(budget=Budget())) == [
        "removes the $10 limit: no spending limit"]
    reasons = loosening(current, SettingsUpdate(budget=Budget(max_cost_usd=20.0),
                                                permissions=PermissionPolicy.ALLOW_READ_ONLY))
    assert len(reasons) == 2
    assert reasons[1] == "lets the agent do more without asking: read freely, ask before changes"


def test_amounts_read_like_money():
    assert dollars(10.0) == "$10" and dollars(2.5) == "$2.50"
    assert dollars(None) == "no spending limit"
    assert model_label(ModelInfo("h", "Haiku", 1_000, input_usd_per_mtok=0.5,
                                 output_usd_per_mtok=2.5)) == (
        "Haiku  ($0.50 in / $2.50 out per million tokens)")
    assert model_label(ModelInfo("x", "Plain", 1_000)) == "Plain"
