import unittest

from kayden.interfaces import FrontendInterface
from kayden.modules.frontend.gui import Module as GuiFrontend


class GuiFrontendTests(unittest.TestCase):
    def test_uses_desktop_window_settings(self):
        frontend = GuiFrontend()
        self.assertIsInstance(frontend, FrontendInterface)
        self.assertEqual(frontend.title, "Kayden")
        self.assertEqual(frontend.geometry, "860x680")
        self.assertFalse(frontend.floating)

    def test_public_events_are_marshaled_to_gui_thread(self):
        frontend = GuiFrontend()
        frontend.user("hello")
        frontend.assistant("hi")
        frontend.finish()
        self.assertEqual(frontend._events.get_nowait(), ("user", "hello"))
        self.assertEqual(frontend._events.get_nowait(), ("assistant", "hi"))
        self.assertEqual(frontend._events.get_nowait(), ("finish", False))

    def test_manual_capture_callbacks_and_drafts_are_marshaled(self):
        frontend = GuiFrontend()
        callbacks = [lambda: None, lambda: None, lambda: None]

        self.assertTrue(frontend.configure_capture(*callbacks, label="Mic"))
        frontend.capture_state("recording")
        frontend.draft("hello world")

        self.assertEqual(frontend._capture_label, "Mic")
        self.assertEqual(frontend._events.get_nowait(), ("capture_state", "recording"))
        self.assertEqual(frontend._events.get_nowait(), ("draft", "hello world"))


if __name__ == "__main__":
    unittest.main()
