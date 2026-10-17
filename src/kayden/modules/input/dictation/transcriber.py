from array import array
import math
import sys


def _cuda_runtime_error(error):
    message = str(error).casefold()
    return any(name in message for name in (
        "libcublas",
        "libcudnn",
        "libcuda",
        "cuda failed",
        "cuda-capable",
    ))


class FasterWhisperTranscriber:
    """Transcribe one utterance with VAD and conservative hallucination filters."""

    def __init__(
        self,
        model="small.en",
        device="cpu",
        compute_type="int8",
        min_seconds=0.35,
        min_rms=0.008,
        engine=None,
    ):
        whisper_model = None
        if engine is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as error:
                raise RuntimeError(
                    "dictation requires the voice-dictation extra: "
                    "python -m pip install 'kayden[voice-dictation]'"
                ) from error
            whisper_model = WhisperModel
            try:
                engine = whisper_model(model, device=device, compute_type=compute_type)
            except RuntimeError as error:
                if device.casefold() == "cpu" or not _cuda_runtime_error(error):
                    raise
                device = "cpu"
                compute_type = "int8"
                engine = whisper_model(model, device=device, compute_type=compute_type)
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self._whisper_model = whisper_model
        self.engine = engine
        self.min_seconds = min_seconds
        self.min_rms = min_rms

    @staticmethod
    def _samples(audio_bytes):
        samples = array("h")
        samples.frombytes(audio_bytes)
        if sys.byteorder != "little":
            samples.byteswap()
        return samples

    def _usable(self, samples, sample_rate):
        if len(samples) / sample_rate < self.min_seconds or not samples:
            return False
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
        return rms / 32768.0 >= self.min_rms

    def _run(self, audio):
        segments, _ = self.engine.transcribe(
            audio,
            language="en",
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )
        return " ".join(segment.text.strip() for segment in segments if segment.text.strip())

    def _can_retry_on_cpu(self, error):
        if self.device.casefold() == "cpu" or self._whisper_model is None:
            return False
        return _cuda_runtime_error(error)

    def transcribe(self, audio_bytes, sample_rate=16000):
        samples = self._samples(audio_bytes)
        if not self._usable(samples, sample_rate):
            return ""

        try:
            import numpy as np
        except ImportError as error:
            raise RuntimeError(
                "dictation requires the voice-dictation extra: "
                "python -m pip install 'kayden[voice-dictation]'"
            ) from error

        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            return self._run(audio)
        except RuntimeError as error:
            if not self._can_retry_on_cpu(error):
                raise
            self.engine = self._whisper_model(
                self.model,
                device="cpu",
                compute_type="int8",
            )
            self.device = "cpu"
            self.compute_type = "int8"
            return self._run(audio)
