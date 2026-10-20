import threading
import unittest

from kayden.contributions import ControlContribution
from kayden.frontend_api import (
    FrontendAPI,
    InvokeControl,
    PermissionRequested,
    StatusChanged,
    SubmitMessage,
)
from kayden.permissions import (
    InvocationContext,
    OperationDefinition,
    PermissionBroker,
    OperationPreview,
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

    def test_sensitive_control_is_authorized_before_its_handler_runs(self):
        called = []

        class Input:
            def contributions(self):
                return [ControlContribution(
                    id="input.test.erase",
                    label="Erase",
                    kind="button",
                    invoke=lambda value: called.append(value),
                    operation=OperationDefinition(
                        "input.test.erase",
                        "Erase input data",
                        default_decision="deny",
                    ),
                )]

        modules = {"input": Input()}
        registry = type("Registry", (), {
            "has": lambda self, name: name in modules,
            "get": lambda self, name: modules[name],
        })()
        runtime = ApplicationRuntime(registry)
        runtime.permissions = PermissionBroker()
        runtime._refresh_controls()

        with self.assertRaisesRegex(PermissionError, "input.test.erase"):
            runtime.invoke_control("input.test.erase")

        self.assertEqual(called, [])

    def test_permission_request_round_trips_through_frontend_api(self):
        modules = {}
        registry = type("Registry", (), {
            "has": lambda self, name: name in modules,
            "get": lambda self, name: modules[name],
        })()
        runtime = ApplicationRuntime(registry)
        received = []
        published = threading.Event()

        class Frontend:
            def _publish(self, event):
                received.append(event)
                published.set()

        runtime._frontend = Frontend()
        operation = OperationDefinition(
            "tool.test.confirm",
            "Confirm test",
            effects=frozenset({"test.write"}),
            default_decision="ask",
            preview=OperationPreview(
                kind="diff",
                title="Proposed changes",
                content="-old\n+new",
            ),
        )
        completed = []

        def authorize():
            completed.append(runtime.permissions.authorize(
                operation,
                {"target": "one"},
                InvocationContext("model"),
            ))

        thread = threading.Thread(target=authorize)
        thread.start()
        self.assertTrue(published.wait(timeout=1))
        request = received[0]
        self.assertIsInstance(request, PermissionRequested)
        self.assertEqual(request.arguments, {"target": "one"})
        self.assertEqual(request.origin, "model")
        self.assertEqual(request.preview_kind, "diff")
        self.assertEqual(request.preview_content, "-old\n+new")

        runtime.resolve_permission(request.request_id, True)
        thread.join(timeout=1)

        self.assertEqual(completed, [True])


if __name__ == "__main__":
    unittest.main()
