import unittest

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

if __name__ == "__main__":
    unittest.main()
