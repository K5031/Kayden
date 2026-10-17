import unittest

from kayden.interfaces import MemoryView
from kayden.modules.context.cloud import Module as CloudContext


class CloudContextTests(unittest.TestCase):
    def test_prepare_does_not_own_or_mutate_messages(self):
        context = CloudContext()
        messages = [{"role": "user", "content": "hello"}]
        view = MemoryView("", messages)

        prepared = context.prepare(view)

        self.assertEqual(prepared, messages)
        self.assertIsNot(prepared, messages)

    def test_reported_or_estimated_usage_triggers_compaction(self):
        context = CloudContext(max_context_tokens=3100)
        messages = [{"role": "user", "content": "a" * 400}]

        self.assertTrue(context.needs_compaction(MemoryView("", messages)))

        context.report_usage(200)
        self.assertTrue(context.needs_compaction(MemoryView("", [])))

    def test_compacted_and_reset_only_change_token_accounting(self):
        context = CloudContext(max_context_tokens=10000)
        context.report_usage(9000)

        context.compacted(MemoryView("short summary", []))
        self.assertLess(context.last_known_tokens, 9000)

        context.reset()
        self.assertEqual(context.last_known_tokens, 0)


if __name__ == "__main__":
    unittest.main()
