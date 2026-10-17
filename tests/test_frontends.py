import unittest

from interfaces import FrontendInterface
from modules.frontend.floaty import Module as FloatyFrontend
from modules.frontend.gui import Module as GuiFrontend
from modules.frontend.tui import Module as TuiFrontend


class FrontendTests(unittest.TestCase):
    def test_all_builtin_frontends_share_the_frontend_contract(self):
        for frontend in (TuiFrontend(), GuiFrontend(), FloatyFrontend()):
            self.assertIsInstance(frontend, FrontendInterface)

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

    def test_floaty_is_compact_and_always_on_top(self):
        frontend = FloatyFrontend()

        self.assertTrue(frontend.floating)
        self.assertEqual(frontend.geometry, "520x330")


if __name__ == "__main__":
    unittest.main()
