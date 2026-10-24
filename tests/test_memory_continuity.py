from pathlib import Path
import sqlite3
import tempfile
import unittest

from kayden.interfaces import ConsolidationResult
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

    def test_retrieves_explicit_text_memory_without_waiting_for_compaction(self):
        self.assertTrue(self.memory.remember_text("The user prefers concise replies."))
        self.assertFalse(self.memory.remember_text("The user prefers concise replies."))

        self.assertEqual(
            self.memory.view().long_term_memory,
            "- The user prefers concise replies.",
        )
        self.assertEqual(
            self.memory.recall("anything"),
            "[no matching historical summaries]",
        )

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

    def test_compaction_replaces_summary_and_memory_then_deletes_covered_raw(self):
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
            result = ConsolidationResult(
                summary="- Switched the project to SQLite.",
                memory="- The project was once called Moonbeam.",
                historical_summary="The Moonbeam project context was retained.",
            )
            self.assertTrue(memory.commit_compaction(plan, result))

            self.assertEqual(memory.view().summary, result.summary)
            self.assertEqual(memory.memory_document(), result.memory)
            self.assertEqual(
                memory.backend.historical_summaries()[0]["summary"],
                result.historical_summary,
            )
            self.assertEqual(memory.view().messages, [
                {"role": "user", "content": "Now use SQLite"},
                {"role": "assistant", "content": "That removes startup work"},
            ])
            stored = memory.backend.messages_after(0)
            self.assertNotIn("The old codename is Moonbeam", {
                message["content"] for message in stored
            })

    def test_archive_write_failure_rolls_back_every_compaction_change(self):
        self.memory.remember_text("Existing durable memory.")
        self.memory.append([
            {"role": "user", "content": "old message"},
            {"role": "assistant", "content": "old response"},
            {"role": "user", "content": "recent message"},
        ])
        plan = self.memory.plan_compaction(1)
        with sqlite3.connect(self.memory.backend.path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_history BEFORE INSERT ON "
                "historical_summaries BEGIN SELECT RAISE(ABORT, "
                "'simulated archive failure'); END"
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.memory.commit_compaction(
                plan,
                ConsolidationResult(
                    "replacement summary",
                    "replacement memory",
                    "historical record",
                ),
            )

        view = self.memory.view()
        self.assertEqual(view.summary, "")
        self.assertEqual(
            view.long_term_memory, "- Existing durable memory."
        )
        self.assertEqual(len(view.messages), 3)
        self.assertEqual(self.memory.backend.historical_summaries(), [])

    def test_retired_history_setting_is_accepted_and_new_archive_is_permanent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuity.sqlite3"
            memory = ContinuityMemory(
                database_path=path,
                compacted_history_messages=200,
            )
            memory.append([
                {"role": "user", "content": "old searchable detail"},
                {"role": "assistant", "content": "old acknowledgement"},
                {"role": "user", "content": "recent question"},
                {"role": "assistant", "content": "recent answer"},
            ])
            plan = memory.plan_compaction(2)
            self.assertTrue(memory.commit_compaction(
                plan,
                ConsolidationResult(
                    "older summary",
                    "important memory",
                    "The user mentioned an old searchable detail.",
                ),
            ))

            self.assertNotIn("old searchable detail", {
                message["content"] for message in memory.backend.messages_after(0)
            })
            self.assertEqual(
                memory.recent(),
                [
                    {"role": "user", "content": "recent question"},
                    {"role": "assistant", "content": "recent answer"},
                ],
            )
            self.assertIn(
                "old searchable detail",
                memory.recall("searchable detail"),
            )

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
            result = ConsolidationResult(
                "one and two", "durable state", "The first exchange occurred."
            )

            self.assertTrue(memory.commit_compaction(plan, result))
            self.assertEqual(memory.view().messages, [
                {"role": "user", "content": "three"},
                {"role": "assistant", "content": "four"},
                {"role": "user", "content": "arrived later"},
            ])
            self.assertFalse(memory.commit_compaction(
                plan,
                ConsolidationResult(
                    "stale overwrite", "lost state", "A stale historical entry."
                ),
            ))
            self.assertEqual(memory.memory_document(), "durable state")
            self.assertEqual(len(memory.backend.historical_summaries()), 1)

    def test_each_archive_entry_covers_only_its_new_compaction_segment(self):
        self.memory.append([
            {"role": "user", "content": "We chose SQLite for storage."},
            {"role": "assistant", "content": "That decision is recorded."},
            {"role": "user", "content": "Keep this exchange recent."},
        ])
        first = self.memory.plan_compaction(1)
        self.assertTrue(self.memory.commit_compaction(
            first,
            ConsolidationResult(
                "The project uses SQLite.",
                "- The project uses SQLite.",
                "The user and assistant chose SQLite for storage.",
            ),
        ))
        self.memory.append([
            {"role": "assistant", "content": "Still recent."},
            {"role": "user", "content": "Later we discussed encryption."},
        ])
        second = self.memory.plan_compaction(1)
        self.assertTrue(self.memory.commit_compaction(
            second,
            ConsolidationResult(
                "The project uses SQLite and encryption was discussed.",
                "- The project uses SQLite.",
                "The retained exchange was followed by an acknowledgement.",
            ),
        ))

        archive = self.memory.backend.historical_summaries()
        self.assertEqual(len(archive), 2)
        self.assertLess(
            archive[0]["through_message_id"], archive[1]["from_message_id"]
        )
        self.assertIn("SQLite", self.memory.recall("storage SQLite"))
        self.assertNotIn("encryption", archive[0]["summary"])

    def test_prepare_and_parse_consolidation_uses_three_bounded_text_outputs(self):
        plan = self.memory.plan_compaction(2)
        self.assertIsNone(plan)
        self.memory.remember_text("The user likes tea.")
        self.memory.append([
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ])
        plan = self.memory.plan_compaction(1)

        request = self.memory.prepare_compaction(plan, 4000, 2000, 1000)
        joined = "\n".join(message["content"] for message in request)
        self.assertIn("Existing long-term memory", joined)
        self.assertIn("The user likes tea.", joined)
        self.assertIn("hard limit is 4000", joined)
        self.assertIn("hard limit is 2000", joined)
        self.assertIn("hard limit is 1000", joined)

        parsed = self.memory.parse_compaction(
            "<conversation_summary>Current work</conversation_summary>\n"
            "<long_term_memory>- The user likes tea.</long_term_memory>\n"
            "<historical_summary>The user discussed one and two."
            "</historical_summary>"
        )
        self.assertEqual(parsed.summary, "Current work")
        self.assertEqual(parsed.memory, "- The user likes tea.")
        self.assertEqual(
            parsed.historical_summary, "The user discussed one and two."
        )
        with self.assertRaisesRegex(ValueError, "missing consolidation"):
            self.memory.parse_compaction("just a summary")

    def test_migrates_existing_key_value_rows_into_text_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuity.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE memories ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                    "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                connection.execute(
                    "INSERT INTO memories (key, value) VALUES (?, ?)",
                    ("profile.university", "UCL"),
                )

            memory = ContinuityMemory(database_path=path)
            self.assertEqual(
                memory.memory_document(),
                "- profile.university: UCL",
            )
            reopened = ContinuityMemory(database_path=path)
            self.assertEqual(
                reopened.memory_document(),
                "- profile.university: UCL",
            )

    def test_text_memory_actions_add_list_and_forget_exact_lines(self):
        actions = self.memory.actions()
        self.assertEqual(
            actions.execute(
                "remember", {"content": "The user studies Computer Science."}
            ),
            "Remembered.",
        )
        self.assertEqual(
            actions.execute("list_memories", {}),
            "- The user studies Computer Science.",
        )
        self.assertEqual(
            actions.execute(
                "forget_memory",
                {"content": "The user studies Computer Science."},
            ),
            "Forgot it.",
        )
        self.assertEqual(
            actions.execute("list_memories", {}),
            "[no durable memories]",
        )

    def test_search_history_action_queries_archived_summaries(self):
        self.memory.append([
            {"role": "user", "content": "We discussed cyberspace streams."},
            {"role": "assistant", "content": "It was an unusual idea."},
            {"role": "user", "content": "Keep this recent."},
        ])
        plan = self.memory.plan_compaction(1)
        self.assertTrue(self.memory.commit_compaction(
            plan,
            ConsolidationResult(
                "The user had an idea about cyberspace.",
                "",
                "The user described cyberspace as information streams.",
            ),
        ))

        result = self.memory.actions().execute(
            "search_history", {"query": "cyberspace streams", "limit": 3}
        )

        self.assertIn("information streams", result)
        self.assertIn("messages 1–2", result)

    def test_historical_recall_has_an_independent_total_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = ContinuityMemory(
                database_path=Path(directory) / "continuity.sqlite3",
                max_recall_characters=256,
            )
            memory.append([
                {"role": "user", "content": "archive topic"},
                {"role": "assistant", "content": "archive response"},
                {"role": "user", "content": "recent"},
            ])
            plan = memory.plan_compaction(1)
            self.assertTrue(memory.commit_compaction(
                plan,
                ConsolidationResult(
                    "archive topic was discussed",
                    "",
                    "archive " + ("detailed history " * 100),
                ),
            ))

            recalled = memory.recall("archive")

            self.assertLessEqual(len(recalled), 256)
            self.assertTrue(recalled.startswith("Historical summary 1"))

    def test_forgetting_one_memory_is_allowed_by_default(self):
        operation = self.memory.actions().operation(
            "forget_memory",
            {"content": "The user likes tea."},
        )

        self.assertEqual(operation.id, "tool.forget_memory")
        self.assertEqual(operation.default_decision, "allow")
        self.assertEqual(operation.effects, frozenset({"memory.delete"}))

    def test_remember_requires_one_concise_text_argument(self):
        actions = self.memory.actions()
        with self.assertRaisesRegex(ValueError, "only content"):
            actions.execute("remember", {"key": "profile.name", "value": "Kai"})
        with self.assertRaisesRegex(ValueError, "non-empty"):
            actions.execute("remember", {"content": "  "})
        with self.assertRaisesRegex(ValueError, "too long"):
            actions.execute("remember", {"content": "x" * 1001})

    def test_remember_schema_uses_natural_text_not_keys(self):
        remember = next(
            schema["function"]
            for schema in self.memory.actions().schemas()
            if schema["function"]["name"] == "remember"
        )
        properties = remember["parameters"]["properties"]
        self.assertEqual(set(properties), {"content"})
        self.assertIn("natural language", properties["content"]["description"])


if __name__ == "__main__":
    unittest.main()
