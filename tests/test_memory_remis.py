import logging
import queue
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from kayden.interfaces import MemoryInterface
from memory_contract import MemoryContractTests


_fake_chromadb = ModuleType("chromadb")
_fake_chromadb.PersistentClient = Mock()
_fake_torch = ModuleType("torch")
_fake_torch.cuda = SimpleNamespace(is_available=lambda: False)
_fake_sentence_transformers = ModuleType("sentence_transformers")
_fake_sentence_transformers.SentenceTransformer = Mock()

with patch.dict(sys.modules, {
    "chromadb": _fake_chromadb,
    "torch": _fake_torch,
    "sentence_transformers": _fake_sentence_transformers,
}):
    from kayden.modules.memory.remis import main as remis


RemisMemory = remis.Module


class _Response:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


def _memory():
    memory = RemisMemory.__new__(RemisMemory)
    memory.name = "Remis"
    memory.log = logging.getLogger("test-remis")
    memory.user_id = "user"
    memory.base_url = "http://localhost:8080"
    memory.dedup_distance = 0.05
    memory.conflict_band_upper = 0.35
    memory.server = None
    memory._queue = queue.Queue()
    memory._worker = None
    memory._stopped = False
    memory.collection = Mock()
    memory.embedder = Mock()
    memory._initialize_transcript()
    return memory


class RemisMemoryTests(MemoryContractTests, unittest.TestCase):
    def setUp(self):
        self.memory = _memory()

    def test_implements_memory_interface(self):
        self.assertTrue(issubclass(RemisMemory, MemoryInterface))

    def test_extracts_only_non_empty_string_facts(self):
        self.assertEqual(
            remis._extract_json_facts(
                'prefix {"facts": ["User likes tea", "", 3]} suffix'
            ),
            ["User likes tea"],
        )
        self.assertEqual(remis._extract_json_facts("not json"), [])

    def test_append_only_queues_turns_containing_user_messages(self):
        self.memory.append([{"role": "assistant", "content": "hello"}])
        self.assertTrue(self.memory._queue.empty())

        turn = [{"role": "user", "content": "remember tea"}]
        self.memory.append(turn)

        self.assertEqual(self.memory._queue.get_nowait(), turn)

    def test_recall_filters_distant_results(self):
        self.memory.collection.count.return_value = 3
        self.memory.collection.query.return_value = {
            "metadatas": [[
                {"data": "User likes tea"},
                {"data": "Too distant"},
                {"data": "User writes Python"},
            ]],
            "distances": [[0.1, 0.8, 0.4]],
        }
        self.memory._embed = Mock(return_value=[0.1, 0.2])

        recalled = self.memory.recall("What does the user like?")

        self.assertEqual(
            recalled,
            "- User likes tea\n- User writes Python",
        )
        self.memory.collection.query.assert_called_once_with(
            query_embeddings=[[0.1, 0.2]],
            n_results=3,
            where={"user_id": "user"},
        )

    def test_extraction_adds_new_facts_skips_duplicates_and_replaces_conflicts(self):
        response = _Response(
            '{"facts": ["new fact", "duplicate", "updated fact"]}'
        )
        self.memory._embed = Mock(side_effect=[[1.0], [2.0], [3.0]])
        self.memory._find_nearest = Mock(side_effect=[
            None,
            ("duplicate-id", "duplicate", 0.01),
            ("old-id", "old fact", 0.2),
        ])
        self.memory._check_conflict = Mock(return_value="CONFLICT")
        self.memory._add_fact = Mock()

        with patch.object(remis.requests, "post", return_value=response):
            stored = self.memory._extract_and_save([
                {"role": "user", "content": "some durable facts"},
            ])

        self.assertEqual(stored, ["new fact", "updated fact"])
        self.assertEqual(self.memory._add_fact.call_args_list, [
            call("new fact", [1.0]),
            call("updated fact", [3.0]),
        ])
        self.memory.collection.delete.assert_called_once_with(ids=["old-id"])

    def test_stop_is_idempotent_without_a_worker(self):
        self.memory._stop_server = Mock()

        self.memory.stop()
        self.memory.stop()

        self.memory._stop_server.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
