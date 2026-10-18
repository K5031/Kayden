import json
from pathlib import Path
import tempfile
import unittest

from kayden.modules.memory.continuity import ContinuityStore
from kayden.modules.memory.continuity import Module as ContinuityMemory
from memory_contract import MemoryContractTests


class ContinuityMemoryTests(MemoryContractTests, unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.memory = ContinuityMemory(
            database_path=Path(self._temporary_directory.name) / "contract.sqlite3"
        )

    def test_retrieves_explicit_memories_without_automatic_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuity.sqlite3"
            store = ContinuityStore(path)
            store.remember("preference.tone", "Concise")
            memory = ContinuityMemory(database_path=path)

            memory.append([
                {"role": "user", "content": "This transient message is not a memory"},
            ])

            self.assertEqual(memory.recall("anything"), "- preference.tone: Concise")
            self.assertEqual(len(store.memories()), 1)

    def test_transcript_survives_a_new_memory_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuity.sqlite3"
            first = ContinuityMemory(database_path=path)
            first.append([
                {"role": "user", "content": "My project is called Moonbeam"},
                {"role": "assistant", "content": "Got it"},
            ])

            second = ContinuityMemory(database_path=path)

            self.assertEqual(second.view().messages, [
                {"role": "user", "content": "My project is called Moonbeam"},
                {"role": "assistant", "content": "Got it"},
            ])

    def test_compaction_keeps_recent_context_and_searchable_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuity.sqlite3"
            memory = ContinuityMemory(database_path=path)
            memory.append([
                {"role": "user", "content": "The old codename is Moonbeam"},
                {"role": "assistant", "content": "I will retain that context"},
                {"role": "user", "content": "Now use SQLite"},
                {"role": "assistant", "content": "That removes startup work"},
            ])

            plan = memory.plan_compaction(2)
            self.assertEqual(len(plan.messages), 2)
            self.assertTrue(memory.commit_compaction(plan, "- Codename: Moonbeam"))
            view = memory.view()
            self.assertEqual(view.summary, "- Codename: Moonbeam")
            self.assertEqual(view.messages, [
                {"role": "user", "content": "Now use SQLite"},
                {"role": "assistant", "content": "That removes startup work"},
            ])

            result = json.loads(memory.actions().execute(
                "search_history",
                {"query": "old codename"},
            ))
            self.assertEqual(result[0]["content"], "The old codename is Moonbeam")

    def test_compaction_plan_does_not_consume_messages_appended_later(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = ContinuityMemory(
                database_path=Path(directory) / "continuity.sqlite3"
            )
            memory.append([
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
                {"role": "assistant", "content": "four"},
            ])
            plan = memory.plan_compaction(2)
            memory.append([{"role": "user", "content": "arrived later"}])

            self.assertTrue(memory.commit_compaction(plan, "one and two"))

            self.assertEqual(memory.view().messages, [
                {"role": "user", "content": "three"},
                {"role": "assistant", "content": "four"},
                {"role": "user", "content": "arrived later"},
            ])
            self.assertFalse(memory.commit_compaction(plan, "stale overwrite"))

    def test_remember_updates_and_forgets_named_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = ContinuityMemory(
                database_path=Path(directory) / "continuity.sqlite3"
            )

            self.assertEqual(
                memory.actions().execute(
                    "remember", {"key": "profile.name", "value": "Kai"}
                ),
                "Remembered profile.name.",
            )
            memory.actions().execute(
                "remember", {"key": "profile.name", "value": "Kay"}
            )
            self.assertEqual(
                memory.actions().execute("list_memories", {}),
                "profile.name: Kay",
            )
            self.assertEqual(
                memory.actions().execute("forget_memory", {"key": "profile.name"}),
                "Forgot profile.name.",
            )
            self.assertEqual(
                memory.actions().execute("list_memories", {}),
                "[no durable memories]",
            )

    def test_rejects_unstable_memory_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = ContinuityMemory(
                database_path=Path(directory) / "continuity.sqlite3"
            )
            with self.assertRaisesRegex(ValueError, "key must start"):
                memory.actions().execute(
                    "remember", {"key": "Profile Name", "value": "Kai"}
                )

    def test_remember_requires_an_atomic_value_instead_of_a_user_sentence(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = ContinuityMemory(
                database_path=Path(directory) / "continuity.sqlite3"
            )

            with self.assertRaisesRegex(ValueError, "atomic value"):
                memory.actions().execute(
                    "remember",
                    {
                        "key": "profile.degree_subject",
                        "value": "User studies Computer Science at UCL.",
                    },
                )

            self.assertEqual(
                memory.actions().execute(
                    "remember",
                    {"key": "profile.degree_subject", "value": "Computer Science"},
                ),
                "Remembered profile.degree_subject.",
            )

    def test_remember_schema_explains_how_to_split_overlapping_facts(self):
        remember = next(
            schema["function"]
            for schema in self.memory.actions().schemas()
            if schema["function"]["name"] == "remember"
        )
        value_description = remember["parameters"]["properties"]["value"]["description"]

        self.assertIn("profile.university='UCL'", value_description)
        self.assertIn("profile.degree_subject='Computer Science'", value_description)


if __name__ == "__main__":
    unittest.main()
