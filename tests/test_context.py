import unittest

from modules.context.cloud import Module as CloudContext


class CloudContextTests(unittest.TestCase):
    def test_old_messages_are_trimmed_after_reported_usage_nears_limit(self):
        context = CloudContext(max_context_tokens=10)
        context.report_usage(20)
        context.add("user", "a" * 14)
        context.add("assistant", "b" * 14)

        self.assertEqual(context.get(), [{"role": "assistant", "content": "b" * 14}])

    def test_clear_resets_messages_and_usage(self):
        context = CloudContext()
        context.report_usage(100)
        context.add("user", "hello")
        context.clear()

        self.assertEqual(context.get(), [])
        self.assertEqual(context.last_known_tokens, 0)


if __name__ == "__main__":
    unittest.main()
