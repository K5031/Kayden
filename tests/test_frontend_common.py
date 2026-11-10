"""Settings texts and the loosening check shared by the TUI and the desktop app."""

from kayden.api import Budget, ModelInfo, ModuleChoice, PermissionPolicy, Settings, SettingsUpdate
from kayden.frontends.common import (
    dollars,
    loosening,
    model_label,
    module_options,
    needs_restart,
    restart_notice,
)
from kayden.runtime.instance import ALREADY_OPEN
from tests.fakes.settings import chosen, settings


def _settings(limit: float | None = 10.0,
              permissions: PermissionPolicy = PermissionPolicy.ASK) -> Settings:
    return settings(budget=Budget(max_cost_usd=limit), permissions=permissions)


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
        "removes the $10 spending limit"]
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


def test_module_lists_say_whether_a_key_is_saved():
    s = settings()
    assert module_options(s.modules["core"]) == [("Claude  (key saved)", "claude"),
                                                 ("Local AI", "local")]
    gone = ModuleChoice("sqlite", "gone", s.modules["memory"].installed)
    assert module_options(gone)[-1] == ("gone  (not installed)", "gone")


def test_module_settings_need_a_restart():
    assert needs_restart(SettingsUpdate(modules={"core": "local"}))
    assert needs_restart(SettingsUpdate(api_key="k")) and needs_restart(
        SettingsUpdate(work_folder="/x"))
    assert not needs_restart(SettingsUpdate(model="m2", budget=Budget()))


def test_restart_notices_say_where_the_chosen_frontend_will_be_used():
    base = settings()
    assert restart_notice(base) is None
    assert restart_notice(settings(restart_needed=True)) == (
        "Your changes take effect after a restart.")
    to_desktop = chosen(base, "frontend", "desktop")
    assert restart_notice(settings(modules=to_desktop, restart_needed=True,
                                   restart_frontend="desktop")) == (
        "Your changes take effect after a restart. Restart now to switch to Desktop app.")
    assert restart_notice(settings(modules=to_desktop, restart_needed=True)) == (
        "Desktop app will be used the next time you start Kayden on a computer with a display. "
        "Your changes take effect after a restart.")
    window = {**base.modules, "frontend": ModuleChoice(
        "desktop", "tui", base.modules["frontend"].installed)}
    assert restart_notice(settings(modules=window, restart_needed=True,
                                   restart_frontend="desktop")).startswith(
        "Terminal (TUI) will be used the next time you run kayden in a terminal.")
    assert restart_notice(settings(startup_error="no key")) == (
        "Fix the settings above, then press Restart now.")
    assert restart_notice(settings(startup_error=f"{ALREADY_OPEN} (/d is in use)")) == (
        "Close the other Kayden window, then press Restart now.")


def test_a_prompt_says_what_the_call_does_and_where():
    from kayden.api import PermissionRequest, ToolCall
    from kayden.frontends.common import PERMISSION_LABELS, request_note

    call = ToolCall("c", "shell", {})
    notes = {(read_only, confined): request_note(PermissionRequest("p", call, read_only, confined))
             for read_only in (True, False) for confined in (True, False)}
    assert notes[False, False] == "It may change things, and can reach outside the work folder."
    assert notes[True, True] == "It only reads, inside the work folder."
    assert len(set(notes.values())) == 4
    assert next(iter(PERMISSION_LABELS)) is PermissionPolicy.ALLOW_WORKSPACE  # listed first
