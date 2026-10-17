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


if __name__ == "__main__":
    unittest.main()
