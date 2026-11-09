"""Settings as a runtime reports them, for frontend tests."""

from __future__ import annotations

from typing import Any

from kayden.api import Budget, ModuleChoice, ModuleInfo, PermissionPolicy, Settings

MODULES = {
    "core": ModuleChoice("claude", "claude", [
        ModuleInfo("claude", "Claude", "ANTHROPIC_API_KEY", True),
        ModuleInfo("local", "Local AI")]),
    "memory": ModuleChoice("sqlite", "sqlite", [
        ModuleInfo("sqlite", "Built-in memory"), ModuleInfo("notes", "Notes memory")]),
    "tools": ModuleChoice("shell", "shell", [ModuleInfo("shell", "Shell and files")]),
    "frontend": ModuleChoice("tui", "tui", [
        ModuleInfo("tui", "Terminal (TUI)", kind="terminal"),
        ModuleInfo("desktop", "Desktop app", kind="window")]),
}


def settings(**changes: Any) -> Settings:
    values: dict[str, Any] = {
        "modules": MODULES, "model": "m1", "maintenance_model": None,
        "budget": Budget(max_cost_usd=10.0), "permissions": PermissionPolicy.ALLOW_READ_ONLY,
        "system_prompt": "Be helpful.", "work_folder": "/work", "restart_needed": False,
        "restart_frontend": "tui"}
    return Settings(**{**values, **changes})


def chosen(base: Settings, kind: str, module_id: str) -> dict[str, ModuleChoice]:
    """`base`'s modules with `module_id` chosen for `kind`."""
    choice = base.modules[kind]
    return {**base.modules, kind: ModuleChoice(choice.running, module_id, choice.installed)}
