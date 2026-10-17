import io
import unittest
from unittest.mock import patch

from kayden import cli


class CliTests(unittest.TestCase):
    def test_doctor_reports_devices(self):
        facts = {
            "input": ["2: USB microphone"],
            "input_default": 2,
            "output": ["4: USB speakers"],
            "output_default": (2, 4),
            "errors": [],
        }
        stream = io.StringIO()
        with patch.object(cli, "audio_diagnostics", return_value=facts), patch("sys.stdout", stream):
            result = cli.main(["doctor"])
        self.assertEqual(result, 0)
        self.assertIn("2: USB microphone", stream.getvalue())
        self.assertIn("4: USB speakers", stream.getvalue())

    def test_doctor_fails_when_no_devices_are_visible(self):
        facts = {"input": [], "output": [], "errors": []}
        with patch.object(cli, "audio_diagnostics", return_value=facts), patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main(["doctor"]), 1)


if __name__ == "__main__":
    unittest.main()
