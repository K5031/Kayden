import logging
import unittest

from kayden.klogger import get_logger


class LoggerTests(unittest.TestCase):
    def test_kayden_logging_does_not_enable_third_party_info_logs(self):
        root = logging.getLogger()
        original_level = root.level
        root.setLevel(logging.WARNING)
        self.addCleanup(root.setLevel, original_level)

        logger = get_logger("Test")

        self.assertEqual(root.level, logging.WARNING)
        self.assertEqual(logger.getEffectiveLevel(), logging.INFO)
        self.assertFalse(logger.propagate)
        self.assertGreaterEqual(logging.getLogger("httpx").getEffectiveLevel(), logging.WARNING)


if __name__ == "__main__":
    unittest.main()
