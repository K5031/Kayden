"""The TUI settings screen (F2 or /settings) and its confirmation step."""

from __future__ import annotations

from textual.widgets import Button, Input, Select, TextArea

from kayden.api import Budget, PermissionPolicy, SettingsUpdate
from kayden.frontends.tui import (
    ConfirmScreen,
    KaydenApp,
    RestartScreen,
    SettingsScreen,
    StartScreen,
)
from tests.conftest import eventually
from tests.fakes.settings import chosen
from tests.fakes.settings import settings as fake_settings
from tests.test_tui import StubRuntime, caught_up, make, type_text, until
from tests.test_tui import make as make_app


async def open_settings(pilot, app: KaydenApp, stub: StubRuntime, how: str = "f2") -> None:
    await caught_up(pilot, app, stub)
    if how == "f2":
        await pilot.press("f2")
    else:
        await type_text(pilot, "/settings")
        await pilot.press("enter")
    await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
    await pilot.pause()


def field(app: KaydenApp, selector: str, kind: type):
    return app.screen.query_one(selector, kind)


async def save(pilot, app: KaydenApp) -> None:
    app.screen.query_one("#save", Button).press()
    await pilot.pause()


def updates(stub: StubRuntime) -> list[SettingsUpdate]:
    return [c[1] for c in stub.calls if c[0] == "update_settings"]


async def test_settings_screen_shows_the_current_settings():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub, how="command")
        assert field(app, "#module-core", Select).value == "claude"
        assert not app.screen.query("#module-memory")  # configured in config.toml only
        assert field(app, "#module-frontend", Select).value == "tui"
        assert field(app, "#api-key", Input).password
        assert field(app, "#api-key", Input).value == ""
        assert field(app, "#model", Select).value == "m1"
        assert field(app, "#spending-limit", Input).value == "10"
        assert field(app, "#permissions", Select).value == PermissionPolicy.ALLOW_READ_ONLY
        assert field(app, "#instructions", TextArea).text == "Be helpful."
        assert field(app, "#work-folder", Input).value == "/work"
        assert "$1.25 spent so far" in str(app.screen.query_one("#spent").render())


async def test_saving_sends_only_what_changed():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        field(app, "#instructions", TextArea).text = "Be brief."
        field(app, "#api-key", Input).value = "sk-new"
        field(app, "#model", Select).value = "m2"
        await save(pilot, app)
        await until(pilot, lambda: updates(stub))
        assert updates(stub) == [SettingsUpdate(api_key="sk-new", model="m2",
                                                system_prompt="Be brief.")]
        assert not isinstance(app.screen, SettingsScreen)


async def test_saving_unchanged_settings_sends_nothing():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        await save(pilot, app)
        await until(pilot, lambda: not isinstance(app.screen, SettingsScreen))
        await pilot.pause()
        assert updates(stub) == []


async def test_escape_cancels():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        field(app, "#instructions", TextArea).text = "Be brief."
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, SettingsScreen))
        await pilot.pause()
        assert updates(stub) == []


async def test_switching_provider_leaves_the_model_to_it():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        field(app, "#module-core", Select).value = "local"
        await pilot.pause()
        assert field(app, "#model", Select).disabled
        await save(pilot, app)
        await until(pilot, lambda: updates(stub))
        assert updates(stub) == [SettingsUpdate(modules={"core": "local"})]


async def test_choosing_the_desktop_app_and_restarting_now():
    """The owner's switch: settings, the desktop app as interface, Restart now."""
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        field(app, "#module-frontend", Select).value = "desktop"
        await pilot.pause()
        restart = app.screen.query_one("#restart", Button)
        assert restart.display
        stub.current_settings = fake_settings(
            modules=chosen(stub.current_settings, "frontend", "desktop"),
            restart_needed=True, restart_frontend="desktop")
        restart.press()
        await until(pilot, lambda: ("restart",) in stub.calls)
        assert updates(stub) == [SettingsUpdate(modules={"frontend": "desktop"})]
        await until(pilot, lambda: not app.is_running)


async def test_saving_a_module_change_offers_restart_now():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        field(app, "#module-frontend", Select).value = "desktop"
        stub.current_settings = fake_settings(
            modules=chosen(stub.current_settings, "frontend", "desktop"),
            restart_needed=True, restart_frontend="tui")
        await save(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, RestartScreen))
        message = str(app.screen.query_one("#restart-message").render())
        assert "Desktop app will be used the next time you start Kayden on a computer with a display" \
            in message
        app.screen.query_one("#restart-later", Button).press()
        await until(pilot, lambda: not isinstance(app.screen, RestartScreen))
        assert ("restart",) not in stub.calls


async def test_saving_without_a_memory_choice_keeps_the_memory():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        field(app, "#module-frontend", Select).value = "desktop"
        await save(pilot, app)
        await until(pilot, lambda: updates(stub))
        assert updates(stub) == [SettingsUpdate(modules={"frontend": "desktop"})]


async def test_a_startup_error_opens_the_settings():
    """First run (no key yet) or a broken module: settings with the error, not a crash."""
    from kayden.api import RuntimeStateError

    stub = StubRuntime(started=False)
    stub.current_settings = fake_settings(startup_error="ANTHROPIC_API_KEY is not set")
    stub.fail_with["list_models"] = RuntimeStateError("Kayden could not start")
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
        await pilot.pause()
        assert "ANTHROPIC_API_KEY is not set" in str(
            app.screen.query_one("#startup-error").render())
        assert app.screen.query_one("#restart", Button).display
        assert field(app, "#model", Select).disabled  # no models before the Core is built
        field(app, "#api-key", Input).value = "sk-1"
        app.screen.query_one("#restart", Button).press()
        await until(pilot, lambda: ("restart",) in stub.calls)
        assert updates(stub) == [SettingsUpdate(api_key="sk-1")]
        assert not any(isinstance(s, StartScreen) for s in app.screen_stack)


async def test_raising_the_spending_limit_asks_first():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        field(app, "#spending-limit", Input).value = "50"
        await save(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert "$10 to $50" in str(app.screen.query_one("#confirm-message").render())
        app.screen.query_one("#confirm-no", Button).press()
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
        assert updates(stub) == []

        await save(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        app.screen.query_one("#confirm-yes", Button).press()
        await until(pilot, lambda: updates(stub))
        assert updates(stub) == [SettingsUpdate(budget=Budget(max_cost_usd=50.0))]


async def test_removing_the_limit_and_loosening_permissions_ask_first():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        field(app, "#spending-limit", Input).value = ""
        field(app, "#permissions", Select).value = PermissionPolicy.ASK
        await save(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert "removes the $10 spending limit" in str(app.screen.query_one("#confirm-message").render())
        app.screen.query_one("#confirm-yes", Button).press()
        await until(pilot, lambda: updates(stub))
        assert updates(stub) == [SettingsUpdate(budget=Budget(),
                                                permissions=PermissionPolicy.ASK)]


async def test_tightening_does_not_ask():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        field(app, "#spending-limit", Input).value = "5"
        field(app, "#permissions", Select).value = PermissionPolicy.DENY
        await save(pilot, app)
        await until(pilot, lambda: updates(stub))
        assert updates(stub) == [SettingsUpdate(budget=Budget(max_cost_usd=5.0),
                                                permissions=PermissionPolicy.DENY)]


async def test_an_invalid_spending_limit_keeps_the_screen_open():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        field(app, "#spending-limit", Input).value = "ten dollars"
        await save(pilot, app)
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert updates(stub) == []


async def test_a_rejected_update_is_reported():
    from kayden.api import RuntimeStateError

    stub = StubRuntime()
    stub.fail_with["update_settings"] = RuntimeStateError("wait until the agent is idle")
    app = make(stub)
    notes: list[str] = []
    app.notify = lambda message, **kw: notes.append(str(message))  # type: ignore[method-assign]
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        field(app, "#work-folder", Input).value = "/elsewhere"
        await save(pilot, app)
        await until(pilot, lambda: any("idle" in n for n in notes))


async def test_settings_screen_fits_a_small_terminal():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(80, 24)) as pilot:
        await open_settings(pilot, app, stub)
        save_button = app.screen.query_one("#save", Button)
        save_button.focus()  # scrolls the dialog, with an animation
        await until(pilot, lambda: app.screen.region.contains_region(save_button.region))



async def test_a_core_from_the_config_file_shows_as_custom():
    from kayden.api import ModuleChoice

    stub = StubRuntime()
    base = stub.current_settings
    stub.current_settings = fake_settings(modules={
        **base.modules, "core": ModuleChoice(None, None, base.modules["core"].installed)})
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        assert field(app, "#module-core", Select).is_blank()
        await save(pilot, app)
        await until(pilot, lambda: not isinstance(app.screen, SettingsScreen))
        assert updates(stub) == []


async def test_tool_servers_are_added_and_removed():
    from dataclasses import replace

    from kayden.api import McpServer
    from kayden.frontends.tui import McpScreen, ServerScreen

    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        await type_text(pilot, "/mcp")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, McpScreen))
        app.screen.query_one("#add", Button).press()
        await until(pilot, lambda: isinstance(app.screen, ServerScreen))
        screen = app.screen
        assert "trust" in str(screen.query_one("#warning").content)
        screen.query_one("#server-name", Input).value = "github"
        screen.query_one("#server-target", Input).value = "npx -y server-github"
        screen.query_one("#server-variables", TextArea).text = "GITHUB_TOKEN=ghp-1"
        stub.current_settings = replace(stub.current_settings, restart_needed=True, mcp_servers=(
            McpServer("github", command="npx -y server-github", env={"GITHUB_TOKEN": ""}),))
        screen.query_one("#save", Button).press()
        await until(pilot, lambda: isinstance(app.screen, McpScreen) and updates(stub))
        assert updates(stub)[-1] == SettingsUpdate(mcp_servers=[McpServer(
            "github", command="npx -y server-github", env={"GITHUB_TOKEN": "ghp-1"})])
        await until(pilot, lambda: app.screen.query_one("#restart", Button).display)
        app.screen.query_one("#remove", Button).press()
        await pilot.pause()
        app.screen.query_one("#confirm-yes", Button).press()
        await until(pilot, lambda: len(updates(stub)) == 2)
        assert updates(stub)[-1] == SettingsUpdate(mcp_servers=[])


async def test_duty_commands_turn_a_duty_on_and_off():
    from kayden.api import Duty, DutyKind

    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        await type_text(pilot, "/goal Launch the campaign")
        await pilot.press("enter")
        await until(pilot, lambda: ("set_duty", Duty("Launch the campaign", DutyKind.GOAL))
                    in stub.calls)
        await type_text(pilot, "/duty off")
        await pilot.press("enter")
        await until(pilot, lambda: ("set_duty", None) in stub.calls)


async def test_the_duty_screen_saves_the_duty_and_plan():
    from kayden.api import Duty, DutyKind, PlanItem, PlanStatus
    from kayden.frontends.tui import DutyScreen

    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 50)) as pilot:
        await caught_up(pilot, app, stub)
        await pilot.press("f5")
        await until(pilot, lambda: isinstance(app.screen, DutyScreen))
        screen = app.screen
        screen.query_one("#duty-instruction", Input).value = "Answer the inbox"
        screen.query_one("#duty-max", Input).value = "30"
        screen.query_one("#duty-plan", TextArea).text = "[doing] Read mail | check VIPs\nReply"
        await save(pilot, app)
        await until(pilot, lambda: any(c[0] == "set_duty" for c in stub.calls))
        assert ("set_plan", [PlanItem("Read mail", PlanStatus.DOING, note="check VIPs"),
                             PlanItem("Reply")]) in stub.calls
        assert ("set_duty", Duty("Answer the inbox", DutyKind.DUTY, 60, 1800)) in stub.calls


async def test_an_unreadable_plan_is_shown_and_nothing_is_saved():
    from kayden.frontends.tui import DutyScreen

    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 50)) as pilot:
        await caught_up(pilot, app, stub)
        await type_text(pilot, "/duty")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, DutyScreen))
        app.screen.query_one("#duty-plan", TextArea).text = "[later] someday"
        await save(pilot, app)
        assert "status must be" in str(app.screen.query_one("#duty-error").render())
        assert not any(c[0] in ("set_duty", "set_plan") for c in stub.calls)


async def test_a_question_for_help_is_shown():
    from kayden.api import HelpNeeded
    from kayden.frontends.tui import SystemEntry
    from tests.test_tui import entries, plain

    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        stub.emit(HelpNeeded("Which account?"))
        await caught_up(pilot, app, stub)
        assert "Which account?" in plain(entries(app, SystemEntry)[-1])
        assert app.help_needed == "Which account?"


async def test_the_sessions_screen_opens_a_child_to_approve_and_talk_to(
        make, tmp_path, child_env):
    from kayden.frontends.tui import SessionsScreen
    from tests.fakes.core import Step, call
    from tests.test_sub_sessions import finish, parent

    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Look it up"}, id="s")]),
        Step(text="Started."), Step(text="Reported.")],
        [{"calls": [{"name": "lookup"}]}, finish("Looked it up."), {"text": "Done."},
         {"text": "Noted."}],
        child_runtime={"runtime": {"permissions": "ask"}})
    await h.turn("Go")
    await eventually(lambda: sessions.sessions and sessions.sessions[0].approvals, timeout=30)
    stub = StubRuntime()
    app = make_app(stub, sessions=sessions)
    async with app.run_test(size=(100, 50)) as pilot:
        await caught_up(pilot, app, stub)
        app.push_screen(SessionsScreen(sessions, h.conversation.id))
        await until(pilot, lambda: app.screen.query("#session-approve")
                    and app.screen.query_one("#session-approve").display, timeout=10)
        assert "lookup" in str(app.screen.query_one("#session-approval").render())
        app.screen.query_one("#allow-session", Button).press()
        await until(pilot, lambda: sessions.sessions[0].state == "done", timeout=20)
        await until(pilot, lambda: "Done." in str(
            app.screen.query_one("#session-lines").render()), timeout=10)
        box = app.screen.query_one("#session-message", Input)
        box.focus()
        box.value = "Thanks"
        await pilot.press("enter")
        await until(pilot, lambda: "Thanks" in str(
            app.screen.query_one("#session-lines").render()), timeout=10)
        app.screen.dismiss(None)
        await pilot.pause()
    await sessions.aclose()
