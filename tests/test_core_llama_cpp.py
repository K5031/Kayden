import logging
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from kayden.events import CoreTextDelta


fake_llama_cpp = ModuleType("llama_cpp")
fake_llama_cpp.Llama = Mock
with patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp}):
    from kayden.modules.core import llama_cpp


def _core(llm):
    core = llama_cpp.Module.__new__(llama_cpp.Module)
    core.log = logging.getLogger("test-llama")
    core.llm = llm
    core._interrupted = False
    return core


class LlamaCppCoreTests(unittest.TestCase):
    def test_initializes_resolved_model(self):
        model = Mock()
        with patch.object(llama_cpp, "resolve_model_path", return_value="/models/test.gguf"), patch.object(llama_cpp, "Llama", return_value=model) as factory:
            core = llama_cpp.Module(model_path="test.gguf", n_gpu_layers=3, n_ctx=4096)
        self.assertIs(core.llm, model)
        self.assertEqual(core.model_path, "/models/test.gguf")
        self.assertEqual(factory.call_args.kwargs["n_gpu_layers"], 3)
        self.assertEqual(factory.call_args.kwargs["n_ctx"], 4096)

    def test_streams_nonempty_text_deltas(self):
        llm = Mock()
        llm.create_chat_completion.return_value = iter([
            {"choices": [{"delta": {"content": "hello"}}]},
            {"choices": [{"delta": {"content": ""}}]},
            {"choices": [{"delta": {"content": " world"}}]},
        ])
        messages = [{"role": "user", "content": "hi"}]
        self.assertEqual(
            list(_core(llm).stream(messages, max_output_tokens=2048)),
            [CoreTextDelta("hello"), CoreTextDelta(" world")],
        )
        self.assertEqual(llm.create_chat_completion.call_args.kwargs["messages"], messages)
        self.assertEqual(llm.create_chat_completion.call_args.kwargs["max_tokens"], 2048)

    def test_rejects_tools(self):
        with self.assertRaisesRegex(NotImplementedError, "does not support tools"):
            list(_core(Mock()).stream([], tools=[{"type": "function"}]))

    def test_interrupt_resets_generation(self):
        llm = Mock()
        llm.create_chat_completion.return_value = iter([{"choices": [{"delta": {"content": "too late"}}]}])
        events = list(_core(llm).stream([], should_interrupt=lambda: True))
        self.assertEqual(events, [])
        llm.reset.assert_called_once_with()

    def test_stop_closes_model(self):
        llm = Mock()
        _core(llm).stop()
        llm.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
