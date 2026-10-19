import logging
import importlib
import builtins
import queue
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch


fake_sounddevice = ModuleType("sounddevice")
fake_sounddevice.OutputStream = Mock
fake_kokoro = ModuleType("kokoro")
fake_kokoro.KPipeline = Mock
with patch.dict(sys.modules, {"sounddevice": fake_sounddevice, "kokoro": fake_kokoro}):
    from kayden.modules.output import kokoro


def _output():
    output = kokoro.Module.__new__(kokoro.Module)
    output.log = logging.getLogger("test-kokoro")
    output.voice = "test-voice"
    output.buffer = ""
    output.queue = queue.Queue()
    output._generation = 0
    output.running = False
    output.thread = None
    output.stream = None
    output._stopped = False
    output._sounddevice = fake_sounddevice
    output._ensure_started = Mock()
    return output


class KokoroOutputTests(unittest.TestCase):
    def test_import_does_not_load_optional_speech_dependencies(self):
        imported = []
        original = builtins.__import__

        def track(name, globals=None, locals=None, fromlist=(), level=0):
            imported.append(name)
            return original(name, globals, locals, fromlist, level)

        spec = importlib.util.spec_from_file_location(
            "_kayden_kokoro_import_test",
            kokoro.__file__,
        )
        module = importlib.util.module_from_spec(spec)
        with patch.object(builtins, "__import__", side_effect=track):
            spec.loader.exec_module(module)

        self.assertNotIn("kokoro", imported)
        self.assertNotIn("sounddevice", imported)

    def test_prepares_pipeline_then_opens_output_lazily(self):
        pipeline = Mock()
        stream = Mock()
        thread = Mock()
        with patch.object(kokoro, "ensure_audio_ready"), patch.object(
            kokoro, "resolve_output_device", return_value=7
        ) as resolve, patch.object(
            kokoro,
            "_load_dependencies",
            return_value=(fake_sounddevice, Mock(return_value=pipeline)),
        ) as load_dependencies, patch.object(
            fake_sounddevice, "OutputStream", return_value=stream
        ), patch.object(kokoro.threading, "Thread", return_value=thread):
            output = kokoro.Module(device="speaker")

            pipeline_factory = load_dependencies.return_value[1]
            pipeline_factory.assert_called_once_with(
                lang_code="a", repo_id="hexgrad/Kokoro-82M", device=None
            )
            resolve.assert_called_once_with(device="speaker")
            stream.start.assert_not_called()
            thread.start.assert_not_called()

            output._ensure_started()

        stream.start.assert_called_once_with()
        thread.start.assert_called_once_with()
        self.assertIs(output.pipeline, pipeline)

    def test_sentence_punctuation_queues_buffer(self):
        output = _output()
        output.send("Hello")
        self.assertTrue(output.queue.empty())
        output.send("!")
        self.assertEqual(output.queue.get_nowait(), (0, "Hello!"))
        self.assertEqual(output.buffer, "")

    def test_markdown_is_removed_from_spoken_text(self):
        output = _output()
        output.send("**Important.**")
        self.assertEqual(output.queue.get_nowait(), (0, "Important."))
        self.assertEqual(output.buffer, "")

    def test_links_lists_and_inline_code_are_spoken_naturally(self):
        output = _output()
        output.buffer = "- Read [`profile.name`](https://example.com) & **remember** it"
        output.flush()
        self.assertEqual(
            output.queue.get_nowait(),
            (0, "Read profile.name & remember it"),
        )

    def test_formatting_only_does_not_start_audio(self):
        output = _output()
        output.buffer = "***"
        output.flush()
        output._ensure_started.assert_not_called()
        self.assertTrue(output.queue.empty())

    def test_flush_queues_trailing_text(self):
        output = _output()
        output.buffer = "unfinished"
        output.flush()
        self.assertEqual(output.queue.get_nowait(), (0, "unfinished"))
        self.assertEqual(output.buffer, "")

    def test_interrupt_invalidates_and_clears_pending_audio(self):
        output = _output()
        output.buffer = "partial"
        output.queue.put((0, "old sentence"))
        output.interrupt()
        self.assertEqual(output._generation, 1)
        self.assertEqual(output.buffer, "")
        self.assertTrue(output.queue.empty())

    def test_stop_closes_audio_stream(self):
        output = _output()
        output.stream = Mock()
        output.stop()
        output.stream.stop.assert_called_once_with()
        output.stream.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
