import unittest

from kayden.contributions import ControlContribution
from kayden.frontend_api import (
    FrontendAPI,
    InvokeControl,
    StatusChanged,
    SubmitMessage,
)
from kayden.runtime.application import ApplicationRuntime


class FrontendAPITests(unittest.TestCase):
    def test_replays_configuration_events_published_before_subscription(self):
        commands = []
        events = []
        api = FrontendAPI(commands.append)
        api._publish(StatusChanged("Ready"))

        unsubscribe = api.subscribe(events.append)
        api.send(SubmitMessage("hello"))
        unsubscribe()

        self.assertEqual(events, [StatusChanged("Ready")])
        self.assertEqual(commands, [SubmitMessage("hello")])

    def test_module_control_is_routed_by_id_and_refreshed_from_owner(self):
        state = {"muted": False}

        def set_muted(value):
            state["muted"] = bool(value)

        class Input:
            def contributions(self):
                return [ControlContribution(
                    id="input.test.mute",
                    label="Mute",
                    kind="toggle",
                    value=state["muted"],
                    invoke=set_muted,
                )]

        modules = {"input": Input()}
        registry = type("Registry", (), {
            "has": lambda self, name: name in modules,
            "get": lambda self, name: modules[name],
        })()
        runtime = ApplicationRuntime(registry)
        events = []
        frontend = type("Frontend", (), {"_publish": events.append})()

        runtime._refresh_controls(frontend)
        runtime._frontend = frontend
        runtime.invoke_control("input.test.mute", True)

        self.assertTrue(state["muted"])
        self.assertEqual(events[0].controls[0]["id"], "input.test.mute")
        self.assertFalse(events[0].controls[0]["value"])
        self.assertTrue(events[1].controls[0]["value"])

    def test_control_contributions_require_namespaced_ids(self):
        with self.assertRaisesRegex(ValueError, "namespaced"):
            ControlContribution(
                id="mute",
                label="Mute",
                kind="toggle",
                value=False,
                invoke=lambda value: None,
            )

    def test_invoke_control_is_a_typed_frontend_command(self):
        commands = []
        api = FrontendAPI(commands.append)
        api.send(InvokeControl("input.test.mute", True))
        self.assertEqual(
            commands,
            [InvokeControl("input.test.mute", True)],
        )


if __name__ == "__main__":
    unittest.main()
