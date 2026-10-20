import unittest

from kayden.frontend_api import (
    ControlsConfigured,
    FrontendAPI,
    InvokeControl,
    PermissionRequested,
)
from kayden.interfaces import FrontendInterface
from kayden.modules.frontend.tui import Module as TuiFrontend


class TuiFrontendTests(unittest.TestCase):
    def test_implements_frontend_contract(self):
        self.assertIsInstance(TuiFrontend(), FrontendInterface)

    def test_tui_marshals_streamed_events_into_transcript_state(self):
        frontend = TuiFrontend()

        frontend.status("Model: test")
        frontend.user("hello")
        frontend.status('[tool] current_time({})')
        frontend.assistant("hi ")
        frontend.assistant("there")
        frontend.finish()
        frontend._drain_events()

        self.assertEqual(frontend._messages, [
            ["You", "hello"],
            ["Tool", '[tool] current_time({})'],
            ["Kayden", "hi there"],
        ])
        self.assertEqual(frontend._status, "Ready")
        self.assertFalse(frontend._busy)

    def test_dictation_is_inserted_into_draft_for_review(self):
        frontend = TuiFrontend()
        callbacks = [lambda: None, lambda: None, lambda: None]
        self.assertTrue(frontend.configure_capture(*callbacks, label="Mic"))

        frontend.capture_state("recording")
        frontend.draft("hello")
        frontend.draft("world")
        frontend.capture_state("idle")
        frontend._drain_events()

        self.assertEqual(frontend._editor, "hello world")
        self.assertEqual(frontend._capture_state, "idle")
        self.assertEqual(frontend._status, "Ready")

    def test_uses_configured_assistant_name(self):
        frontend = TuiFrontend()
        frontend.configure_identity("Ahri")
        frontend.assistant("hello")
        frontend._drain_events()

        self.assertEqual(frontend._assistant_name, "Ahri")
        self.assertEqual(frontend._messages, [["Ahri", "hello"]])

    def test_exposes_generic_controls_as_terminal_commands(self):
        commands = []
        frontend = TuiFrontend()
        frontend._api = FrontendAPI(commands.append)
        frontend._accept_event(ControlsConfigured([{
            "id": "input.whisper.mute",
            "label": "Mute microphone",
            "kind": "toggle",
            "value": False,
            "enabled": True,
            "icon": "mic",
            "placement": "header",
            "options": [],
        }]))

        self.assertTrue(frontend._run_control_command("/mute"))
        self.assertEqual(
            commands,
            [InvokeControl("input.whisper.mute", True)],
        )

    def test_queues_permission_request_for_keyboard_confirmation(self):
        frontend = TuiFrontend()
        request = PermissionRequested(
            "permission-1",
            "tool.forget_memory",
            "Forget memory",
            {"key": "profile.name"},
            ("memory.delete",),
            "model",
            False,
        )

        frontend._accept_event(request)
        frontend._drain_events()

        self.assertEqual(frontend._permission, request)
        self.assertEqual(frontend._status, "Allow Forget memory?  y/n")

if __name__ == "__main__":
    unittest.main()
