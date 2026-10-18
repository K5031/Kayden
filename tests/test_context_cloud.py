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
        context = CloudContext(
            max_context_tokens=3100,
            recent_verbatim_tokens=1500,
            summary_target_tokens=200,
            durable_memory_tokens=100,
            target_free_tokens=1000,
            minimum_free_tokens=500,
        )
        messages = [{"role": "user", "content": "a" * 10000}]

        self.assertTrue(context.needs_compaction(MemoryView("", messages)))

        context.report_usage(2700)
        self.assertTrue(context.needs_compaction(MemoryView("", [])))

    def test_default_budgets_leave_room_for_continuous_conversation(self):
        context = CloudContext()

        self.assertEqual(context.summary_target_tokens, 4000)
        self.assertEqual(context.recent_verbatim_tokens, 35000)
        self.assertEqual(context.durable_memory_tokens, 2000)
        self.assertEqual(context.target_free_tokens, 19000)
        self.assertEqual(context.minimum_free_tokens, 10000)

    def test_retention_uses_a_token_budget_and_keeps_complete_turns(self):
        context = CloudContext(
            max_context_tokens=100,
            recent_verbatim_tokens=15,
            summary_target_tokens=5,
            durable_memory_tokens=5,
            target_free_tokens=20,
            minimum_free_tokens=10,
        )
        messages = [
            {"role": "user", "content": "o" * 14},
            {"role": "assistant", "content": "o" * 14},
            {"role": "user", "content": "n" * 21},
            {"role": "assistant", "content": "n" * 21},
        ]

        self.assertEqual(
            context.retained_message_count(MemoryView("", messages)),
            2,
        )

    def test_summary_is_bounded_to_its_target(self):
        context = CloudContext(
            max_context_tokens=100,
            recent_verbatim_tokens=40,
            summary_target_tokens=4,
            durable_memory_tokens=2,
            target_free_tokens=20,
            minimum_free_tokens=10,
        )

        summary = "first useful line\nsecond line that exceeds the limit"
        bounded = context.constrain_summary(summary)

        self.assertLessEqual(len(bounded), 14)
        self.assertEqual(bounded, "first useful")

    def test_compacted_and_reset_only_change_token_accounting(self):
        context = CloudContext(max_context_tokens=10000)
        context.report_usage(9000)

        context.compacted(MemoryView("short summary", []))
        self.assertLess(context.last_known_tokens, 9000)

        context.reset()
        self.assertEqual(context.last_known_tokens, 0)


if __name__ == "__main__":
    unittest.main()
