import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from kayden.interfaces import MemoryView


fake_llama_cpp = ModuleType("llama_cpp")
fake_llama_cpp.Llama = Mock
with patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp}):
    from kayden.modules.context import local


class LocalContextTests(unittest.TestCase):
    def setUp(self):
        self.tokenizer = Mock()
        self.tokenizer.tokenize.side_effect = lambda value: list(value)
        self.llama_patch = patch.object(local, "Llama", return_value=self.tokenizer)
        self.resolve_patch = patch.object(local, "resolve_model_path", return_value="/models/test.gguf")
        self.llama = self.llama_patch.start()
        self.resolve = self.resolve_patch.start()
        self.addCleanup(self.llama_patch.stop)
        self.addCleanup(self.resolve_patch.stop)

    def test_initializes_a_vocab_only_tokenizer(self):
        context = local.Module(model_path="test.gguf", keep_recent_messages=1)
        self.resolve.assert_called_once_with("test.gguf")
        self.llama.assert_called_once_with(model_path="/models/test.gguf", vocab_only=True, verbose=False)
        self.assertEqual(context.keep_recent_messages, 2)

    def test_prepares_summary_and_copies_messages(self):
        context = local.Module()
        original = {"role": "user", "content": "hello"}
        prepared = context.prepare(MemoryView("earlier facts", [original]))
        self.assertEqual(prepared[0], {
            "role": "system", "content": "Summary of earlier conversation:\nearlier facts",
        })
        self.assertEqual(prepared[1], original)
        self.assertIsNot(prepared[1], original)

    def test_uses_tokenizer_for_compaction_threshold(self):
        context = local.Module(max_context_tokens=10)
        self.assertFalse(context.needs_compaction(MemoryView("", [{"role": "user", "content": "12345678"}])))
        self.assertTrue(context.needs_compaction(MemoryView("", [{"role": "user", "content": "123456789"}])))

    def test_stop_closes_tokenizer(self):
        context = local.Module()
        context.stop()
        self.tokenizer.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
