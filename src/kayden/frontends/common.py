"""Settings texts and the loosening check, shared by every frontend so the rule that
loosening needs the user's confirmation (api.py update_settings) has one implementation."""

from __future__ import annotations

from kayden.api import ModelInfo, ModuleChoice, PermissionPolicy, Settings, SettingsUpdate
from kayden.runtime.instance import ALREADY_OPEN

PERMISSION_LABELS = {
    PermissionPolicy.ASK: "Ask me before every action",
    PermissionPolicy.ALLOW_READ_ONLY: "Read freely, ask before changes",
    PermissionPolicy.DENY: "Never run tools",
}


def money(value: float) -> str:
    return f"${value:g}" if value == int(value) else f"${value:.2f}"


def dollars(value: float | None) -> str:
    return "no spending limit" if value is None else money(value)


def model_label(model: ModelInfo) -> str:
    if model.input_usd_per_mtok is None or model.output_usd_per_mtok is None:
        return model.name
    return (f"{model.name}  ({money(model.input_usd_per_mtok)} in / "
            f"{money(model.output_usd_per_mtok)} out per million tokens)")


def loosening(current: Settings, update: SettingsUpdate) -> list[str]:
    """What in `update` loosens `current`, each as a phrase completing "This change …";
    empty when nothing needs confirming."""
    reasons = []
    if update.budget is not None:
        old, new = current.budget.max_cost_usd, update.budget.max_cost_usd
        if old is not None and (new is None or new > old):
            reasons.append(f"raises the spending limit from {dollars(old)} to {dollars(new)}"
                           if new is not None else
                           f"removes the {dollars(old)} limit: no spending limit")
    if update.permissions is not None:
        order = list(PermissionPolicy)
        if order.index(update.permissions) > order.index(current.permissions):
            reasons.append("lets the agent do more without asking: "
                           + PERMISSION_LABELS[update.permissions].lower())
    return reasons


MODULE_LABELS = {"core": "AI provider", "memory": "Memory", "tools": "Tools",
                 "frontend": "Interface"}
MEMORY_WARNING = ("A different memory keeps its own conversations and memories: the ones "
                  "you have now are not shown after the restart, and come back when you "
                  "switch back.")
AT_RESTART = "The AI provider, key, modules and work folder apply when Kayden restarts."


def module_options(choice: ModuleChoice) -> list[tuple[str, str]]:
    """(label, id) for a module list; a Core says whether its key is saved."""
    options = []
    for m in choice.installed:
        key = "" if m.key_name is None else "  (key saved)" if m.has_key else "  (no key yet)"
        options.append((m.name + key, m.id))
    if choice.chosen is not None and choice.chosen not in {m.id for m in choice.installed}:
        options.append((f"{choice.chosen}  (not installed)", choice.chosen))
    return options


def module_name(choice: ModuleChoice, module_id: str | None) -> str:
    return next((m.name for m in choice.installed if m.id == module_id), module_id or "custom")


def needs_restart(update: SettingsUpdate) -> bool:
    """Whether `update` holds module settings, which apply at the next start."""
    return bool(update.modules) or update.api_key is not None or update.work_folder is not None


def restart_notice(settings: Settings) -> str | None:
    """What waits for a restart, for a line next to Restart now; None when nothing does."""
    if settings.startup_error is not None:
        if settings.startup_error.startswith(ALREADY_OPEN):
            return "Close the other Kayden window, then press Restart now."
        return "Fix the settings above, then restart Kayden."
    if not settings.restart_needed:
        return None
    frontend = settings.modules["frontend"]
    notice = "Saved changes apply when Kayden restarts."
    if frontend.chosen is not None and frontend.chosen != settings.restart_frontend:
        kind = next((m.kind for m in frontend.installed if m.id == frontend.chosen), None)
        where = ("the next time Kayden is started from the app menu" if kind == "window"
                 else "the next time you run kayden in a terminal")
        notice = f"{module_name(frontend, frontend.chosen)} will be used {where}. " + notice
    elif settings.restart_frontend != frontend.running:
        notice += (f" Restart now continues in "
                   f"{module_name(frontend, settings.restart_frontend)}.")
    return notice
