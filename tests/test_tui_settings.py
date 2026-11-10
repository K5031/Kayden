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
from tests.fakes.settings import chosen
from tests.fakes.settings import settings as fake_settings
from tests.test_tui import StubRuntime, caught_up, make, type_text, until


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
        assert field(app, "#module-memory", Select).value == "sqlite"
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
        assert "Desktop app will be used the next time Kayden is started from the app menu" \
            in message
        app.screen.query_one("#restart-later", Button).press()
        await until(pilot, lambda: not isinstance(app.screen, RestartScreen))
        assert ("restart",) not in stub.calls


async def test_a_memory_change_warns_first():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        field(app, "#module-memory", Select).value = "notes"
        await save(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert "not shown after the restart" in str(
            app.screen.query_one("#confirm-message").render())
        app.screen.query_one("#confirm-yes", Button).press()
        await until(pilot, lambda: updates(stub))
        assert updates(stub) == [SettingsUpdate(modules={"memory": "notes"})]


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
        assert "no spending limit" in str(app.screen.query_one("#confirm-message").render())
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
