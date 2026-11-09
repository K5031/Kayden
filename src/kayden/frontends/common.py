"""Settings texts and the loosening check, shared by every frontend so the rule that
loosening needs the user's confirmation (api.py update_settings) has one implementation."""

from __future__ import annotations

from kayden.api import ModelInfo, PermissionPolicy, Settings, SettingsUpdate

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
