from datetime import datetime, timedelta, timezone
import inspect
import importlib
import builtins
import logging
import queue
import threading
import sys
from types import ModuleType
import unittest
from unittest.mock import MagicMock, Mock, patch


fake_numpy = ModuleType("numpy")
fake_numpy.int16 = object()
fake_numpy.float32 = object()
fake_sr = ModuleType("speech_recognition")
fake_sr.AudioData = type("AudioData", (), {})
fake_sr.Microphone = Mock
fake_sr.Recognizer = Mock
fake_whisper = ModuleType("whisper")
fake_whisper.load_model = Mock()
with patch.dict(sys.modules, {
    "numpy": fake_numpy,
    "speech_recognition": fake_sr,
    "whisper": fake_whisper,
}):
    from kayden.modules.input import whisper as whisper_input


def _input():
    module = whisper_input.Module.__new__(whisper_input.Module)
    module.log = logging.getLogger("test-whisper")
    module._np = fake_numpy
    module._speech_recognition = fake_sr
    module.model = Mock()
    module._cuda = False
    module.data_queue = queue.Queue()
    module.output_queue = queue.Queue()
    module.buffer = b""
    module.last_audio_time = None
    module.phrase_timeout = 1
    module.min_seconds = 0.35
    module.min_rms = 0.006
    module.min_avg_logprob = -0.8
    module.max_no_speech_prob = 0.6
    module.max_compression_ratio = 2.4
    module._running = False
    module._stopped = False
    module._thread = None
    module._start_lock = threading.Lock()
    module.energy_threshold = 1000.0
    module.calibrate = True
    module.calibration_duration = 1.0
    module.max_calibrated_threshold = 4000.0
    module.source = MagicMock()
    module.recorder = Mock()
    return module


class WhisperInputTests(unittest.TestCase):
    def test_import_does_not_load_optional_speech_dependencies(self):
        imported = []
        original = builtins.__import__

        def track(name, globals=None, locals=None, fromlist=(), level=0):
            imported.append(name)
            return original(name, globals, locals, fromlist, level)

        spec = importlib.util.spec_from_file_location(
            "_kayden_whisper_import_test",
            whisper_input.__file__,
        )
        module = importlib.util.module_from_spec(spec)
        with patch.object(builtins, "__import__", side_effect=track):
            spec.loader.exec_module(module)

        self.assertNotIn("numpy", imported)
        self.assertNotIn("speech_recognition", imported)
        self.assertNotIn("whisper", imported)

    def test_default_silence_timeout_is_conversational(self):
        parameters = inspect.signature(whisper_input.Module).parameters
        self.assertEqual(parameters["record_timeout"].default, 1.5)
        self.assertEqual(parameters["phrase_timeout"].default, 2.0)
        self.assertLess(
            parameters["record_timeout"].default,
            parameters["phrase_timeout"].default,
        )

    def test_rejects_implausibly_high_ambient_calibration(self):
        module = _input()

        def calibrate(_source, duration):
            self.assertEqual(duration, 1.0)
            module.recorder.energy_threshold = 14_000

        module.recorder.adjust_for_ambient_noise.side_effect = calibrate
        module._calibrate_recorder()

        self.assertEqual(module.recorder.energy_threshold, 1000.0)

    def test_keeps_reasonable_ambient_calibration(self):
        module = _input()

        def calibrate(_source, duration):
            module.recorder.energy_threshold = 650

        module.recorder.adjust_for_ambient_noise.side_effect = calibrate
        module._calibrate_recorder()

        self.assertEqual(module.recorder.energy_threshold, 650)

    def test_calibration_can_be_disabled_for_a_fixed_threshold(self):
        module = _input()
        module.calibrate = False
        module.recorder.energy_threshold = 14_000

        module._calibrate_recorder()

        self.assertEqual(module.recorder.energy_threshold, 1000.0)
        module.recorder.adjust_for_ambient_noise.assert_not_called()

    def test_cuda_probe_handles_missing_driver(self):
        fake_torch = ModuleType("torch")
        fake_torch.cuda = Mock()
        fake_torch.cuda.is_available.return_value = False
        with patch.dict(sys.modules, {"torch": fake_torch}):
            self.assertFalse(whisper_input._cuda_available())
        fake_torch.cuda.is_available.return_value = True
        with patch.dict(sys.modules, {"torch": fake_torch}):
            self.assertTrue(whisper_input._cuda_available())

    def test_transcribe_drops_low_confidence_segments(self):
        module = _input()
        module.model.transcribe.return_value = {"segments": [
            {"text": "keep", "avg_logprob": -0.2},
            {"text": "drop", "avg_logprob": -1.2},
            {"text": "this", "avg_logprob": -0.5},
        ]}
        with patch.object(module, "_audio_to_numpy", return_value="audio"), \
                patch.object(module, "_usable_audio", return_value=True):
            self.assertEqual(module._transcribe(b"bytes"), "keep this")
        self.assertFalse(module.model.transcribe.call_args.kwargs["fp16"])
        self.assertTrue(
            module.model.transcribe.call_args.kwargs["word_timestamps"]
        )

    def test_transcribe_rejects_silence_and_repetitive_segments(self):
        module = _input()
        module.model.transcribe.return_value = {"segments": [
            {"text": "silence", "avg_logprob": -0.2, "no_speech_prob": 0.9},
            {"text": "repeated", "avg_logprob": -0.2, "compression_ratio": 3.0},
            {"text": "spoken", "avg_logprob": -0.2, "no_speech_prob": 0.1},
        ]}
        with patch.object(module, "_audio_to_numpy", return_value="audio"), \
                patch.object(module, "_usable_audio", return_value=True):
            self.assertEqual(module._transcribe(b"bytes"), "spoken")

    def test_transcribe_skips_unusable_audio_before_model_inference(self):
        module = _input()
        with patch.object(module, "_audio_to_numpy", return_value="audio"), \
                patch.object(module, "_usable_audio", return_value=False):
            self.assertEqual(module._transcribe(b"bytes"), "")
        module.model.transcribe.assert_not_called()

    def test_completed_phrase_is_queued(self):
        module = _input()
        module.buffer = b"audio"
        module.last_audio_time = datetime.now(timezone.utc) - timedelta(seconds=2)
        with patch.object(module, "_transcribe", return_value="hello"):
            module._process_audio()
        self.assertEqual(module.output_queue.get_nowait(), "hello")
        self.assertEqual(module.buffer, b"")
        self.assertIsNone(module.last_audio_time)

    def test_combines_pending_transcripts(self):
        module = _input()
        module.output_queue.put("hello")
        module.output_queue.put("world")
        self.assertEqual(module._get_combined_text(), "hello world")
        self.assertFalse(module.has_input())

    def test_stopped_input_unblocks_reader(self):
        module = _input()
        module._stopped = True

        with self.assertRaises(EOFError):
            module.get_input()


if __name__ == "__main__":
    unittest.main()
