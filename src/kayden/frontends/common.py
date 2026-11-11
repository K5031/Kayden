"""What every frontend shows the same way, independent of its toolkit: how steps read
(their sentence and input, past tense, results, folded groups), permission and settings
texts, restart notices and formatting. The loosening check is here too, so the rule that
loosening needs the user's confirmation (api.py update_settings) has one implementation."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from kayden.api import (
    DESCRIPTION_ARG,
    NOTICE_PREFIX,
    SUGGESTION_TOOLS,
    AgentState,
    Duty,
    DutyKind,
    McpServer,
    ModelInfo,
    ModuleChoice,
    PermissionPolicy,
    PermissionRequest,
    PlanItem,
    PlanStatus,
    Proposal,
    ServerToolUse,
    Settings,
    SettingsUpdate,
    ToolCall,
    ToolResult,
    ToolStatus,
)

PERMISSION_LABELS = {
    PermissionPolicy.AUTO: "Decide automatically, ask me only when unsure",
    PermissionPolicy.ALLOW_WORKSPACE: "Work freely in the work folder, ask before anything else",
    PermissionPolicy.ALLOW_READ_ONLY: "Read freely, ask before changes",
    PermissionPolicy.ASK: "Ask me before every action",
    PermissionPolicy.DENY: "Never run tools",
}


@dataclass(frozen=True, slots=True)
class Card:
    """What a SUGGESTION_TOOLS call shows under the answer. `action` is the label of the
    option that runs a call, the card Proposal with id `call_id` while it is pending."""

    call_id: str
    recap: str
    question: str
    options: tuple[str, ...]
    action: str | None


def card_of(call: ToolCall) -> Card | None:
    """The card a call shows, or None."""
    args = call.arguments

    def text(key: str) -> str:
        value = args.get(key)
        return value if isinstance(value, str) else ""

    if call.name == "wrap_up":
        raw = [o for o in args.get("options") or () if isinstance(o, dict)]
        options = tuple(o["label"] for o in raw if isinstance(o.get("label"), str) and o["label"])
        action = next((o["label"] for o in raw if "call" in o and o.get("label")), None)
        if not text("recap") and not options:
            return None
        return Card(call.id, text("recap"), text("question"), options, action)
    if call.name in SUGGESTION_TOOLS:
        replies = args.get("replies")
        options = tuple(r for r in replies if isinstance(r, str) and r) if isinstance(
            replies, list) else ()
        return Card(call.id, "", text("question"), options, None) if options else None
    return None


def notice_of(text: str) -> str | None:
    """What a message Kayden wrote for the model (api.NOTICE_PREFIX) says, to show as a
    notice; None for anything the user wrote."""
    return text.removeprefix(NOTICE_PREFIX).strip() if text.startswith(NOTICE_PREFIX) else None


def proposal_parts(proposal: Proposal) -> tuple[str, str]:
    """A proposal for its card: the headline ("Kayden proposes: …") and the call's other
    arguments."""
    headline, detail = call_parts(proposal.call.arguments)
    if proposal.call.name == "start_session":
        headline = headline or f"Start a sub-session: {proposal.call.arguments.get('task')}"
        detail = ""
    return f"Kayden proposes: {headline or f'using {proposal.call.name}'}", detail


PERMISSION_SHORT = {  # for a picker: a word or two
    PermissionPolicy.AUTO: "Auto",
    PermissionPolicy.ALLOW_WORKSPACE: "Work folder",
    PermissionPolicy.ALLOW_READ_ONLY: "Read only",
    PermissionPolicy.ASK: "Ask first",
    PermissionPolicy.DENY: "No tools",
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
COMPACTING = "Compacting conversation…"
COMPACTED = "Conversation compacted"
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


def delete_details(is_open: bool, sub_sessions: int = 0) -> str:
    """What deleting a conversation does, for its confirmation."""
    details = ("This permanently deletes the conversation. What Kayden remembered from it "
               "stays in Memory.")
    if sub_sessions == 1:
        details += " Its sub-session stops and is deleted too."
    elif sub_sessions:
        details += f" Its {sub_sessions} sub-sessions stop and are deleted too."
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


MCP_WARNING = ("An MCP server is someone else's program running with your permissions: add "
               "only servers you trust. Kayden checks or asks before each of its actions.")
MCP_VARIABLES_HINT = ("One per line, NAME=value, for keys or tokens the server needs. Values "
                      "are kept in your system's keychain; leave a value empty to keep it.")


def mcp_server(name: str, target: str, variables: str) -> McpServer:
    """A server from what the user typed: a URL (http or https) or else a command, and its
    variables as NAME=value lines. The runtime checks the rest."""
    env = {}
    for line in variables.splitlines():
        if not line.strip():
            continue
        variable, sep, value = line.partition("=")
        if not sep:
            raise ValueError(f"write each variable as NAME=value: {line.strip()!r}")
        env[variable.strip()] = value.strip()
    target = target.strip()
    if target.startswith(("http://", "https://")):
        return McpServer(name.strip(), url=target, env=env)
    return McpServer(name.strip(), command=target, env=env)


def mcp_target(server: McpServer) -> str:
    return server.command or server.url or ""


def mcp_variables(server: McpServer) -> str:
    """The saved variables for editing: their names, values left empty (kept)."""
    return "\n".join(f"{name}=" for name in server.env)


def provider_step(use: ServerToolUse) -> tuple[ToolCall, ToolResult]:
    """A tool the provider ran, as a step: its summary is the step's sentence."""
    call = ToolCall(use.id, use.name, {**use.input, DESCRIPTION_ARG: use.summary})
    status = ToolStatus.ERROR if use.is_error else ToolStatus.OK
    return call, ToolResult(use.id, status, use.content)


# ── Duties ──

DUTY_KIND_LABELS = {
    DutyKind.DUTY: "Keep doing it until I turn it off",
    DutyKind.GOAL: "Until it is done",
}
DUTY_HINT = ("Kayden carries this on by itself: it keeps a plan, works on it, and checks in "
             "again on its own. It needs a spending limit, and pauses when you press Stop, "
             "when Kayden restarts, or when the limit is reached.")
PLAN_HINT = ("One step per line: [todo], [doing], [done] or [dropped], the step, then "
             "optionally | due YYYY-MM-DD HH:MM and | a note.")
_PLAN_LINE = re.compile(r"\[(\w+)\]\s*(.*)")


def duty_status(duty: Duty | None, wake_at: float | None, state: AgentState,
                help_needed: str | None = None) -> str | None:
    """The duty's state for a status line, None without a duty."""
    if duty is None:
        return None
    if help_needed is not None and state in (AgentState.IDLE, AgentState.STOPPED):
        return "Duty waiting for your answer"
    if wake_at is not None:
        return f"Duty: next check-in {_when(wake_at)}"
    if state in (AgentState.IDLE, AgentState.STOPPED, AgentState.FAILED):
        return "Duty paused: activate to resume"
    return "Duty: working"


def help_text(question: str) -> str:
    return f"Kayden needs your help to go on with its duty: {question}"


def _when(t: float) -> str:
    when = datetime.fromtimestamp(t)
    return when.strftime("%H:%M" if when.date() == datetime.now().date() else "%Y-%m-%d %H:%M")


def plan_text(plan: Sequence[PlanItem]) -> str:
    """The plan as editable lines (PLAN_HINT)."""
    lines = []
    for item in plan:
        line = f"[{item.status}] {item.text}"
        if item.due is not None:
            line += f" | due {datetime.fromtimestamp(item.due).strftime('%Y-%m-%d %H:%M')}"
        if item.note:
            line += f" | {item.note}"
        lines.append(line)
    return "\n".join(lines)


def parse_plan_text(text: str) -> list[PlanItem]:
    """plan_text's lines back to a plan; ValueError naming the line that cannot be read."""
    plan = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        status, rest = PlanStatus.TODO, line.strip()
        if match := _PLAN_LINE.fullmatch(rest):
            try:
                status = PlanStatus(match[1].lower())
            except ValueError:
                raise ValueError(f"Line {number}: the status must be todo, doing, done or "
                                 "dropped.") from None
            rest = match[2]
        step, *extras = (part.strip() for part in rest.split("|"))
        if not step:
            raise ValueError(f"Line {number} has no step.")
        due, notes = None, []
        for extra in extras:
            if extra.lower().startswith("due "):
                try:
                    due = datetime.strptime(extra[4:].strip(), "%Y-%m-%d %H:%M").timestamp()
                except ValueError:
                    raise ValueError(f"Line {number}: write the due time as YYYY-MM-DD "
                                     "HH:MM.") from None
            elif extra:
                notes.append(extra)
        plan.append(PlanItem(step, status, due, " | ".join(notes)))
    return plan
