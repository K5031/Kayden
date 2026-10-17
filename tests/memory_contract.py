class MemoryContractTests:
    """Behavior every transcript-owning memory module must preserve."""

    def test_contract_appends_messages_without_retaining_mutable_input(self):
        message = {"role": "user", "content": "hello"}

        self.memory.append([message])
        message["content"] = "changed later"

        self.assertEqual(
            self.memory.view().messages,
            [{"role": "user", "content": "hello"}],
        )

    def test_contract_recent_returns_the_latest_messages(self):
        self.memory.append([
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ])

        self.assertEqual(self.memory.recent(2), [
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ])

    def test_contract_compaction_keeps_recent_messages(self):
        self.memory.append([
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
            {"role": "assistant", "content": "four"},
        ])

        plan = self.memory.plan_compaction(2)

        self.assertIsNotNone(plan)
        self.assertEqual([message["content"] for message in plan.messages], [
            "one",
            "two",
        ])
        self.assertTrue(self.memory.commit_compaction(plan, "older summary"))
        self.assertEqual(self.memory.view().summary, "older summary")
        self.assertEqual(
            [message["content"] for message in self.memory.view().messages],
            ["three", "four"],
        )

    def test_contract_reset_starts_fresh_context(self):
        self.memory.append([
            {"role": "user", "content": "preserved in history"},
        ])

        self.memory.reset_context()

        self.assertEqual(self.memory.view().summary, "")
        self.assertEqual(self.memory.view().messages, [])

