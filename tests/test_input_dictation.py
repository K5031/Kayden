from array import array
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kayden.interfaces import ManualInputInterface
from kayden.modules.input.dictation import Module
from kayden.modules.input.dictation.capture import AudioCapture
from kayden.modules.input.dictation.transcriber import FasterWhisperTranscriber


def _pcm(value, seconds=0.5, sample_rate=16000):
    return array("h", [value] * int(seconds * sample_rate)).tobytes()


class _Capture:
    def __init__(self, audio=b"audio"):
        self.audio = audio
        self.events = []

    def start(self):
        self.events.append("start")

    def finish(self):
        self.events.append("finish")
        return self.audio

    def cancel(self):
        self.events.append("cancel")


class _Transcriber:
    def __init__(self, result="spoken words"):
        self.result = result
        self.audio = None

    def transcribe(self, audio):
        self.audio = audio
        return self.result


class _Engine:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        return iter([
            SimpleNamespace(text=" hello "),
            SimpleNamespace(text="world"),
        ]), object()


class _AudioArray:
    def astype(self, dtype):
        return self

    def __truediv__(self, value):
        return self


_NUMPY = SimpleNamespace(
    frombuffer=lambda data, dtype: _AudioArray(),
    int16=object(),
    float32=object(),
)


class DictationTests(unittest.TestCase):
    def test_module_implements_manual_capture_contract(self):
        capture = _Capture()
        transcriber = _Transcriber()
        module = Module(capture=capture, transcriber=transcriber)

        self.assertIsInstance(module, ManualInputInterface)
        self.assertEqual(module.capture_label, "Mic")
        module.start_capture()
        self.assertEqual(module.finish_capture(), "spoken words")
        module.cancel_capture()

        self.assertEqual(capture.events, ["start", "finish", "cancel"])
        self.assertEqual(transcriber.audio, b"audio")

    def test_empty_capture_does_not_load_or_run_transcriber(self):
        capture = _Capture(audio=b"")
        module = Module(capture=capture)

        self.assertEqual(module.finish_capture(), "")
        self.assertIsNone(module._transcriber)

    def test_module_defaults_to_portable_cpu_inference(self):
        created = []

        class Transcriber:
            def __init__(self, **options):
                created.append(options)

            def transcribe(self, audio):
                return "hello"

        with patch(
            "kayden.modules.input.dictation.FasterWhisperTranscriber",
            Transcriber,
        ), patch.dict("os.environ", {}, clear=True):
            module = Module(capture=_Capture())
            self.assertEqual(module.finish_capture(), "hello")

        self.assertEqual(created, [{
            "model": "small.en",
            "device": "cpu",
            "compute_type": "int8",
        }])

    def test_short_and_quiet_audio_are_rejected_before_inference(self):
        engine = _Engine()
        transcriber = FasterWhisperTranscriber(engine=engine)

        self.assertEqual(transcriber.transcribe(_pcm(5000, seconds=0.1)), "")
        self.assertEqual(transcriber.transcribe(_pcm(1)), "")
        self.assertEqual(engine.calls, [])

    def test_transcriber_enables_vad_and_hallucination_guards(self):
        engine = _Engine()
        transcriber = FasterWhisperTranscriber(engine=engine)

        with patch.dict("sys.modules", {"numpy": _NUMPY}):
            self.assertEqual(transcriber.transcribe(_pcm(5000)), "hello world")
        _, options = engine.calls[0]
        self.assertTrue(options["vad_filter"])
        self.assertFalse(options["condition_on_previous_text"])
        self.assertEqual(options["vad_parameters"], {"min_silence_duration_ms": 500})
        self.assertEqual(options["no_speech_threshold"], 0.6)

    def test_missing_cuda_libraries_retry_once_on_cpu(self):
        engines = []

        class BrokenCudaEngine:
            def transcribe(self, audio, **kwargs):
                def fail_during_iteration():
                    raise RuntimeError("Library libcublas.so.12 is not found")
                    yield

                return fail_during_iteration(), object()

        class CpuEngine(_Engine):
            pass

        def whisper_model(model, device, compute_type):
            engines.append((model, device, compute_type))
            return BrokenCudaEngine() if device == "cuda" else CpuEngine()

        fake_module = SimpleNamespace(WhisperModel=whisper_model)
        with patch.dict(
            "sys.modules",
            {"faster_whisper": fake_module, "numpy": _NUMPY},
        ):
            transcriber = FasterWhisperTranscriber(
                model="small.en",
                device="cuda",
                compute_type="default",
            )
            self.assertEqual(transcriber.transcribe(_pcm(5000)), "hello world")

        self.assertEqual(engines, [
            ("small.en", "cuda", "default"),
            ("small.en", "cpu", "int8"),
        ])
        self.assertEqual(transcriber.device, "cpu")

    def test_cuda_model_creation_failure_retries_on_cpu(self):
        engines = []

        def whisper_model(model, device, compute_type):
            engines.append((model, device, compute_type))
            if device == "cuda":
                raise RuntimeError("CUDA failed: no CUDA-capable device")
            return _Engine()

        fake_module = SimpleNamespace(WhisperModel=whisper_model)
        with patch.dict("sys.modules", {"faster_whisper": fake_module}):
            transcriber = FasterWhisperTranscriber(
                model="small.en",
                device="cuda",
                compute_type="default",
            )

        self.assertEqual(engines, [
            ("small.en", "cuda", "default"),
            ("small.en", "cpu", "int8"),
        ])
        self.assertEqual(transcriber.device, "cpu")

    def test_capture_starts_stops_and_releases_pyaudio(self):
        class Stream:
            def __init__(self):
                self.started = False
                self.stopped = False
                self.closed = False

            def start_stream(self):
                self.started = True

            def is_active(self):
                return True

            def stop_stream(self):
                self.stopped = True

            def close(self):
                self.closed = True

        stream = Stream()

        class Audio:
            def __init__(self):
                self.options = None
                self.terminated = False

            def open(self, **kwargs):
                self.options = kwargs
                return stream

            def terminate(self):
                self.terminated = True

        audio = Audio()
        fake_pyaudio = SimpleNamespace(
            PyAudio=lambda: audio,
            paInt16=8,
            paComplete=1,
            paContinue=0,
        )
        capture = AudioCapture(max_seconds=1)

        with patch.dict("sys.modules", {"pyaudio": fake_pyaudio}), patch(
            "kayden.modules.input.dictation.capture.resolve_pyaudio_input_device",
            return_value=4,
        ):
            capture.start()
            callback = audio.options["stream_callback"]
            callback(b"\x01\x00" * 10, 10, None, None)
            result = capture.finish()

        self.assertEqual(result, b"\x01\x00" * 10)
        self.assertEqual(audio.options["input_device_index"], 4)
        self.assertFalse(audio.options["start"])
        self.assertTrue(stream.started)
        self.assertTrue(stream.stopped)
        self.assertTrue(stream.closed)
        self.assertTrue(audio.terminated)


if __name__ == "__main__":
    unittest.main()
