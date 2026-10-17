import io
import unittest
from unittest.mock import patch

from kayden.interfaces import OutputInterface
from kayden.modules.output.terminal import Module as TerminalOutput


class TerminalOutputTests(unittest.TestCase):
    def test_implements_output_contract_and_streams_text(self):
        output = TerminalOutput()
        self.assertIsInstance(output, OutputInterface)
        stream = io.StringIO()
        with patch("sys.stdout", stream):
            output.send("hello")
            output.status("working")
        self.assertEqual(stream.getvalue(), "hello\nworking\n")


if __name__ == "__main__":
    unittest.main()
