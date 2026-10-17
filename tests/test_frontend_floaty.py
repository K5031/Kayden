import unittest

from kayden.interfaces import FrontendInterface
from kayden.modules.frontend.floaty import Module as FloatyFrontend


class FloatyFrontendTests(unittest.TestCase):
    def test_is_compact_and_always_on_top(self):
        frontend = FloatyFrontend()
        self.assertIsInstance(frontend, FrontendInterface)
        self.assertEqual(frontend.title, "Kayden · Floaty")
        self.assertEqual(frontend.geometry, "520x330")
        self.assertTrue(frontend.floating)

    def test_stop_is_marshaled_to_gui_thread(self):
        frontend = FloatyFrontend()
        frontend.stop()
        self.assertEqual(frontend._events.get_nowait(), ("close", None))


if __name__ == "__main__":
    unittest.main()
