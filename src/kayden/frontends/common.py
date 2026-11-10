"""Settings texts and the loosening check, shared by every frontend so the rule that
loosening needs the user's confirmation (api.py update_settings) has one implementation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kayden.api import (
    ModelInfo,
    ModuleChoice,
    PermissionPolicy,
    PermissionRequest,
    Settings,
    SettingsUpdate,
)
from kayden.runtime.instance import ALREADY_OPEN

PERMISSION_LABELS = {
    PermissionPolicy.AUTO: "Decide automatically, ask me only when unsure",
    PermissionPolicy.ALLOW_WORKSPACE: "Work freely in the work folder, ask before anything else",
    PermissionPolicy.ALLOW_READ_ONLY: "Read freely, ask before changes",
    PermissionPolicy.ASK: "Ask me before every action",
    PermissionPolicy.DENY: "Never run tools",
}


def request_note(request: PermissionRequest) -> str:
    """What a call that asks for permission does, and why it asks, for the prompt."""
    if request.confined:
        note = ("It only reads, inside the work folder." if request.read_only
                else "It may change things inside the work folder.")
    else:
        note = ("It only reads, but can reach outside the work folder." if request.read_only
                else "It may change things, and can reach outside the work folder.")
    return f"{note} Why it asks: {request.reason}" if request.reason else note


def call_parts(arguments: Any) -> tuple[str | None, str]:
    """A tool call for display: the plain-language description it gives, if any, and the
    rest of its arguments: a shell command or a single text argument as it is, anything
    else as JSON. Arguments
    still streaming (a string) are shown as they are."""
    if not isinstance(arguments, dict):
        return None, str(arguments)
    rest = {k: v for k, v in arguments.items() if k != "description"}
    description = arguments.get("description")
    headline = description.strip() if isinstance(description, str) else ""
    command = rest.get("command")
    if isinstance(command, str) and set(rest) <= {"command", "unsandboxed"}:
        detail = command + (" (outside the sandbox)" if rest.get("unsandboxed") else "")
    elif len(rest) == 1 and isinstance(only := next(iter(rest.values())), str):
        detail = only  # a fact to remember, a search query, a path
    else:
        detail = json.dumps(rest, ensure_ascii=False)
    return headline or None, detail


def folder_path(folder: str) -> str:
    """A folder for display, with the home folder as ~."""
    path, home = Path(folder), Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else (
        "~" if path == home else folder)


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
                           f"removes the {dollars(old)} spending limit")
    if update.permissions is not None:
        order = list(PermissionPolicy)
        if order.index(update.permissions) > order.index(current.permissions):
            reasons.append("lets the agent do more without asking: "
                           + PERMISSION_LABELS[update.permissions].lower())
    return reasons


# The modules settings offer. Memory is chosen in config.toml only: another Memory module
# is another archive, so a choice in the app would hide every conversation.
MODULE_LABELS = {"core": "AI provider", "tools": "Tools", "frontend": "Interface"}
AT_RESTART = "Changes to the AI provider, API key and modules take effect after a restart."


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


def delete_details(is_open: bool) -> str:
    """What deleting a conversation does, for its confirmation."""
    details = ("This permanently deletes the conversation. What Kayden remembered from it "
               "stays in Memory.")
    return details + (" A new chat will open." if is_open else "")


def needs_restart(update: SettingsUpdate) -> bool:
    """Whether `update` holds module settings, which apply at the next start."""
    return bool(update.modules) or update.api_key is not None


def restart_notice(settings: Settings) -> str | None:
    """What waits for a restart, for a line next to Restart now; None when nothing does."""
    if settings.startup_error is not None:
        if settings.startup_error.startswith(ALREADY_OPEN):
            return "Close the other Kayden window, then press Restart now."
        return "Fix the settings above, then press Restart now."
    if not settings.restart_needed:
        return None
    frontend = settings.modules["frontend"]
    notice = "Your changes take effect after a restart."
    if frontend.chosen is not None and frontend.chosen != settings.restart_frontend:
        kind = next((m.kind for m in frontend.installed if m.id == frontend.chosen), None)
        where = ("the next time you start Kayden on a computer with a display"
                 if kind == "window" else "the next time you run kayden in a terminal")
        notice = f"{module_name(frontend, frontend.chosen)} will be used {where}. " + notice
    elif settings.restart_frontend != frontend.running:
        notice += (f" Restart now to switch to "
                   f"{module_name(frontend, settings.restart_frontend)}.")
    return notice
