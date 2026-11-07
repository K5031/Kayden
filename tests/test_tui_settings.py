"""The TUI settings screen (F2 or /settings) and its confirmation step."""

from __future__ import annotations

from textual.widgets import Button, Input, Select, TextArea

from kayden.api import Budget, PermissionPolicy, SettingsUpdate
from kayden.frontends.tui import ConfirmScreen, KaydenApp, SettingsScreen
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
        assert field(app, "#provider", Select).value == "claude"
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


async def test_switching_provider_uses_its_first_model():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(pilot, app, stub)
        field(app, "#provider", Select).value = "local"
        await pilot.pause()
        assert field(app, "#model", Select).disabled
        await save(pilot, app)
        await until(pilot, lambda: updates(stub))
        assert updates(stub) == [SettingsUpdate(provider="local")]


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


def test_amounts_read_like_money():
    from kayden.api import ModelInfo
    from kayden.frontends.tui import _dollars, _model_label

    assert _dollars(10.0) == "$10" and _dollars(2.5) == "$2.50" and _dollars(None) == (
        "no spending limit")
    assert _model_label(ModelInfo("h", "Haiku", 1_000, input_usd_per_mtok=0.5,
                                  output_usd_per_mtok=2.5)) == (
        "Haiku  ($0.50 in / $2.50 out per million tokens)")
    assert _model_label(ModelInfo("x", "Plain", 1_000)) == "Plain"
