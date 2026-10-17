from datetime import datetime
import unittest

from kayden.modules.tools.clock import Module as ClockTool


class ClockToolTests(unittest.TestCase):
    def test_returns_an_iso_timestamp(self):
        result = ClockTool().execute("current_time", {})
        self.assertIsNotNone(datetime.fromisoformat(result))

    def test_rejects_unknown_tool_and_arguments(self):
        clock = ClockTool()
        with self.assertRaises(ValueError):
            clock.execute("other", {})
        with self.assertRaises(ValueError):
            clock.execute("current_time", {"unexpected": True})


if __name__ == "__main__":
    unittest.main()
