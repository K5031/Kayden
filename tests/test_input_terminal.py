import queue
import unittest

from kayden.modules.input.terminal import Module as TerminalInput


class TerminalInputTests(unittest.TestCase):
    def test_eof_unblocks_reader(self):
        terminal = TerminalInput.__new__(TerminalInput)
        terminal.output_queue = queue.Queue()
        terminal._ensure_started = lambda: None
        terminal.output_queue.put(None)

        with self.assertRaises(EOFError):
            terminal.get_input()


if __name__ == "__main__":
    unittest.main()
