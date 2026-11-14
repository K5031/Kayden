"""Settings texts and the loosening check shared by the TUI and the desktop app."""

import pytest

from kadence.api import (
    Budget,
    ModelInfo,
    ModuleChoice,
    PermissionPolicy,
    Settings,
    SettingsUpdate,
)
from kadence.frontends.common import (
    dollars,
    loosening,
    model_label,
    module_options,
    needs_restart,
    restart_notice,
)
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
    assert needs_restart(SettingsUpdate(api_key="k"))
    assert not needs_restart(SettingsUpdate(work_folder="/x"))  # the next new chat gets it
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
        "Desktop app will be used the next time you start Kadence on a computer with a display. "
        "Your changes take effect after a restart.")
    window = {**base.modules, "frontend": ModuleChoice(
        "desktop", "tui", base.modules["frontend"].installed)}
    assert restart_notice(settings(modules=window, restart_needed=True,
                                   restart_frontend="desktop")).startswith(
        "Terminal (TUI) will be used the next time you run kadence in a terminal.")
    assert restart_notice(settings(startup_error="no key")) == (
        "Fix the settings above, then press Restart now.")
    assert restart_notice(settings(startup_error="open elsewhere", already_open=True)) == (
        "Close the other Kadence window, then press Restart now.")


def test_a_prompt_says_what_the_call_does_and_where():
    from kadence.api import PermissionRequest, ToolCall
    from kadence.frontends.common import PERMISSION_LABELS, request_note

    call = ToolCall("c", "shell", {})
    notes = {(read_only, confined): request_note(PermissionRequest("p", call, read_only, confined))
             for read_only in (True, False) for confined in (True, False)}
    assert notes[False, False] == "It may change things, and can reach outside the work folder."
    assert notes[True, True] == "It only reads, inside the work folder."
    assert len(set(notes.values())) == 4
    reviewed = PermissionRequest("p", call, False, False, "It deletes your photos.")
    assert request_note(reviewed).endswith("folder. Why it asks: It deletes your photos.")
    assert next(iter(PERMISSION_LABELS)) is PermissionPolicy.AUTO  # the default, listed first


def test_a_call_shows_its_description_and_then_its_command():
    from kadence.frontends.common import call_parts

    assert call_parts({"command": "ls -la", "step_summary": "List the files"}) == (
        "List the files", "ls -la")
    assert call_parts({"command": "uv sync", "unsandboxed": True,
                       "step_summary": "Install the packages"}) == (
        "Install the packages", "command: uv sync\nunsandboxed: true")
    assert call_parts({"path": "a.txt", "limit": 5}) == (None, "path: a.txt\nlimit: 5")
    assert call_parts({"command": "true", "step_summary": "  "}) == (None, "true")
    # a tool's own "description" argument is input, not the step's sentence
    assert call_parts({"description": "The body of the issue"}) == (
        None, "The body of the issue")
    assert call_parts('{"command": "ls') == (None, '{"command": "ls')  # still streaming


def test_a_call_with_one_text_argument_shows_just_that_text():
    from kadence.frontends.common import call_parts

    assert call_parts({"text": "Studies CS at UCL"}) == (None, "Studies CS at UCL")
    assert call_parts({"query": "python 3.14"}) == (None, "python 3.14")
    assert call_parts({"path": "notes.md", "offset": 3}) == (None, "path: notes.md\noffset: 3")


def test_a_finished_step_reads_in_the_past_tense():
    from kadence.frontends.common import past_tense

    cases = {
        "Read BBC explainer on the Jensen Huang story": "Read BBC explainer on the Jensen Huang story",
        "Search the web for flights": "Searched the web for flights",
        "Check Kadence's activity log": "Checked Kadence's activity log",
        "Make a folder for your notes": "Made a folder for your notes",
        "Remove the old build": "Removed the old build",
        "Write the summary": "Wrote the summary",
        "Find large files": "Found large files",
        "Run the tests": "Ran the tests",
        "Copy the photos": "Copied the photos",
        "Stop the server": "Stopped the server",
        "Look up UCL's modules": "Looked up UCL's modules",
        "remember that you study CS": "remembered that you study CS",
        "The weather in Oslo": "The weather in Oslo",  # not a known verb: as written
        "": "",
    }
    for sentence, past in cases.items():
        assert past_tense(sentence) == past, sentence


def test_mcp_servers_from_the_form_and_back():
    from kadence.api import McpServer
    from kadence.frontends.common import mcp_server, mcp_target, mcp_variables

    server = mcp_server(" github ", " npx -y server-github ", "GITHUB_TOKEN = ghp-1\n\n")
    assert server == McpServer("github", command="npx -y server-github",
                               env={"GITHUB_TOKEN": "ghp-1"})
    assert mcp_server("docs", "https://d.example/mcp", "").url == "https://d.example/mcp"
    saved = McpServer("github", command="npx s", env={"GITHUB_TOKEN": ""})
    assert mcp_target(saved) == "npx s" and mcp_variables(saved) == "GITHUB_TOKEN="
    with pytest.raises(ValueError, match="NAME=value"):
        mcp_server("x", "cmd", "just-a-name")


def test_plan_text_round_trips_and_reports_bad_lines():
    from datetime import datetime

    import pytest

    from kadence.api import PlanItem, PlanStatus
    from kadence.frontends.common import parse_plan_text, plan_text

    due = datetime(2030, 1, 7, 9, 0).timestamp()
    plan = [PlanItem("Read mail", PlanStatus.DOING, due, "VIPs first"), PlanItem("Reply")]
    assert plan_text(plan) == ("[doing] Read mail | due 2030-01-07 09:00 | VIPs first\n"
                               "[todo] Reply")
    assert parse_plan_text(plan_text(plan) + "\n\n") == plan
    assert parse_plan_text("Call Sam") == [PlanItem("Call Sam")]
    with pytest.raises(ValueError, match="Line 2"):
        parse_plan_text("ok\n[todo] x | due tomorrow")


def test_a_text_only_frontend_names_what_was_attached():
    from kadence.api import File, Image
    from kadence.frontends.common import with_attachments

    attached = [Image("image/png", "AAAA", "shot.png"), File("/w/notes.txt", "Buy oat milk.")]
    assert with_attachments("look", attached) == "look\n[image: shot.png]\n[file: notes.txt]"
    assert with_attachments("", [Image("image/png", "AAAA")]) == "[image]"
    assert with_attachments("plain", []) == "plain"


def test_a_tool_result_names_what_it_returned_to_look_at():
    from kadence.api import Document, Image, ToolResult, ToolStatus
    from kadence.frontends.common import result_text

    result = ToolResult("c", ToolStatus.OK, "[PDF, 3 pages]", (
        Document("/w/report.pdf", "JV", "Sales", 3), Image("image/png", "AAAA", "chart.png")))
    assert result_text(result) == "[PDF, 3 pages]\n[file: report.pdf]\n[image: chart.png]"
    assert result_text(ToolResult("c", ToolStatus.OK, "done")) == "done"


def test_duty_status_says_when_it_wakes_or_that_it_is_paused():
    import time

    from kadence.api import AgentState, Duty
    from kadence.frontends.common import duty_status

    duty = Duty("x")
    assert duty_status(None, None, AgentState.IDLE) is None
    assert duty_status(None, 4102480800.0, AgentState.IDLE) == "Reminder at 2100-01-01 03:00" \
        or duty_status(None, 4102480800.0, AgentState.IDLE).startswith("Reminder at 2")
    assert duty_status(duty, None, AgentState.STOPPED) == "Duty paused: activate to resume"
    assert duty_status(duty, None, AgentState.THINKING) == "Duty: working"
    assert duty_status(duty, time.time() + 60, AgentState.IDLE).startswith(
        "Duty: next check-in ")


def test_delete_details_name_the_sub_sessions():
    from kadence.frontends.common import delete_details

    assert "sub-session" not in delete_details(False)
    assert "Its sub-session stops and is deleted too." in delete_details(False, 1)
    assert "Its 3 sub-sessions stop and are deleted too." in delete_details(True, 3)


def test_a_notice_is_told_by_its_author_not_its_words():
    from kadence.api import Author, Message, MessageQueued, OperationResult, Role, Text, ToolStatus
    from kadence.frontends.common import notice_of

    def message(text: str, **fields) -> Message:
        return Message("m", Role.USER, (Text(text),), 0.0, **fields)

    assert notice_of(message("[Runtime notice] I typed this myself")) is None
    assert notice_of(message(" Woken for the duty. ", author=Author.KADENCE)) == \
        "Woken for the duty."
    assert notice_of(message("The user talks.", author=Author.KADENCE, hidden=True)) == ""
    assert notice_of(Message("o", Role.USER, (OperationResult("c", ToolStatus.OK, "ok"),),
                             0.0, Author.KADENCE)) == ""
    assert notice_of(Message("a", Role.ASSISTANT, (Text("Done."),), 0.0)) is None
    assert notice_of(MessageQueued("q", "hello")) is None
    assert notice_of(MessageQueued("q", "Sub-session 1 ended.", author=Author.KADENCE)) == \
        "Sub-session 1 ended."
