"""What every frontend shows the same way, independent of its toolkit: how steps read
(their sentence and input, past tense, results, folded groups), permission and settings
texts, restart notices and formatting. The loosening check is here too, so the rule that
loosening needs the user's confirmation (api.py update_settings) has one implementation."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from kayden.api import (
    DESCRIPTION_ARG,
    ModelInfo,
    ModuleChoice,
    PermissionPolicy,
    PermissionRequest,
    Settings,
    SettingsUpdate,
    ToolStatus,
)

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


# Verbs a step description starts with, as the model writes them (imperative), and their
# past tense. A sentence starting with any other word is shown as written.
_IRREGULAR = {
    "build": "built", "buy": "bought", "choose": "chose", "do": "did", "draw": "drew",
    "find": "found", "forget": "forgot", "get": "got", "give": "gave", "go": "went", "keep": "kept",
    "leave": "left", "make": "made", "put": "put", "read": "read", "rerun": "reran",
    "run": "ran", "see": "saw", "send": "sent", "set": "set", "show": "showed",
    "take": "took", "tell": "told", "think": "thought", "undo": "undid",
    "understand": "understood", "write": "wrote", "rewrite": "rewrote",
}
_DOUBLED = {"stop": "stopped", "plan": "planned", "drop": "dropped", "commit": "committed",
            "submit": "submitted", "ship": "shipped", "zip": "zipped", "unzip": "unzipped",
            "tag": "tagged", "log": "logged", "pin": "pinned", "map": "mapped"}
_REGULAR = frozenset({
    "add", "analyse", "analyze", "append", "apply", "archive", "ask", "back", "backup",
    "calculate", "cancel", "change", "check", "clean", "clear", "clone", "close", "collect",
    "combine", "compare", "compile", "compress", "configure", "confirm", "convert", "copy",
    "count", "crawl", "create", "debug", "decrypt", "delete", "deploy", "describe", "disable",
    "download", "draft", "edit", "enable", "encrypt", "estimate", "execute", "explain",
    "export", "extract", "fetch", "fill", "filter", "fix", "format", "gather", "generate",
    "grep", "inspect", "install", "launch", "list", "load", "locate", "look", "merge", "move",
    "name", "open", "organise", "organize", "parse", "paste", "patch", "pause", "ping", "play",
    "prepare", "print", "process", "pull", "push", "query", "remember", "remove", "rename",
    "render", "reorganise", "reorganize", "replace", "reply", "report", "request", "research",
    "resize", "restart", "restore", "retry", "review", "save", "scan", "schedule", "scrape",
    "search", "select", "sort", "split", "start", "summarise", "summarize", "sync", "test",
    "track", "transcribe", "translate", "trim", "try", "type", "update", "upgrade", "upload",
    "use", "validate", "verify", "view", "visit", "wait", "watch",
})


def past_tense(sentence: str) -> str:
    """A finished step's description: its first word in the past tense when it is a verb
    Kayden knows ("Search the web" -> "Searched the web"), else the sentence as written."""
    first, _, rest = sentence.partition(" ")
    verb = first.lower()
    if verb in _IRREGULAR:
        past = _IRREGULAR[verb]
    elif verb in _DOUBLED:
        past = _DOUBLED[verb]
    elif verb in _REGULAR:
        if verb.endswith("e"):
            past = verb + "d"
        elif verb.endswith("y") and verb[-2:-1] not in ("a", "e", "o", "u"):
            past = verb[:-1] + "ied"
        else:
            past = verb + "ed"
    else:
        return sentence
    if first[:1].isupper():
        past = past[0].upper() + past[1:]
    return f"{past} {rest}" if rest else past


RESULT_LABELS = {  # how a step ended, in words
    ToolStatus.OK: "done",
    ToolStatus.ERROR: "failed",
    ToolStatus.REFUSED: "declined",
    ToolStatus.CANCELLED: "cancelled",
    ToolStatus.TIMED_OUT: "timed out",
    ToolStatus.ABANDONED: "abandoned, may still be running",
    ToolStatus.INTERRUPTED: "interrupted",
}


def steps_title(count: int) -> str:
    """A finished group of steps, by count: the tools behind them are not known here."""
    return f"{count} step{'s' if count != 1 else ''}"


def step_problems(labels: Iterable[str]) -> str:
    """'2 failed, 1 declined' from each step's problem label ('' for a step that went fine)."""
    counts = Counter(label for label in labels if label)
    return ", ".join(f"{n} {label}" for label, n in counts.items())


def group_title(steps: Sequence[tuple[str, str, str | None]]) -> tuple[str, str, int | None]:
    """A folded group of steps, each (sentence, problem, state while under way or None):
    the title, the state beside it, and the index of the step under way. That is the
    latest step under way; else the one step's sentence and problem; else "N steps" and
    the problems counted."""
    active = next((i for i in range(len(steps) - 1, -1, -1) if steps[i][2] is not None), None)
    if active is not None:
        sentence, _, state = steps[active]
        return sentence, state or "", active
    if len(steps) == 1:
        return steps[0][0], steps[0][1], None
    return steps_title(len(steps)), step_problems(problem for _, problem, _ in steps), None


def call_parts(arguments: Any) -> tuple[str | None, str]:
    """A tool call for display: the plain-language description the model gave it
    (api.DESCRIPTION_ARG), if any, and the rest of its arguments: a single text argument as
    it is, several as "name: value" lines. Arguments still streaming (a string) are shown
    as they are."""
    if not isinstance(arguments, dict):
        return None, str(arguments)
    rest = {k: v for k, v in arguments.items() if k != DESCRIPTION_ARG}
    description = arguments.get(DESCRIPTION_ARG)
    headline = description.strip() if isinstance(description, str) else ""
    if len(rest) == 1 and isinstance(only := next(iter(rest.values())), str):
        detail = only  # a command, a fact to remember, a search query, a path
    else:
        detail = "\n".join(f"{k}: {v if isinstance(v, str) else json.dumps(v)}"
                            for k, v in rest.items())
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
    """Whether `update` holds module settings (modules, the key, MCP servers), which apply
    at the next start."""
    return bool(update.modules) or update.api_key is not None or update.mcp_servers is not None


def restart_notice(settings: Settings) -> str | None:
    """What waits for a restart, for a line next to Restart now; None when nothing does."""
    if settings.startup_error is not None:
        if settings.already_open:
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
