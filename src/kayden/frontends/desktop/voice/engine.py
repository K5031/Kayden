"""Speech on this machine with sherpa-onnx: Silero VAD hears when the user talks,
Parakeet turns it into text, Kokoro speaks. All on the CPU."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import sherpa_onnx

RATE = 16000  # what the microphone is heard at
VAD_FILE = "silero_vad.onnx"
LISTEN_DIR = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
SPEAK_DIR = "kokoro-multi-lang-v1_0"
END_OF_TURN_S = 0.9  # silence that ends what the user is saying
MIN_SPEECH_S = 0.25  # shorter sounds are not speech
VOICE = 3  # Kokoro's "af_heart": an American woman's voice


class SherpaEngine:
    """turns.Engine over the models in `models` (see download.MODELS). Loading takes a
    few seconds and blocks: build it on a thread."""

    def __init__(self, models: Path) -> None:
        threads = max(1, min(8, (os.cpu_count() or 2) // 2))
        vad = sherpa_onnx.VadModelConfig()
        vad.silero_vad.model = str(models / VAD_FILE)
        vad.silero_vad.min_silence_duration = END_OF_TURN_S
        vad.silero_vad.min_speech_duration = MIN_SPEECH_S
        vad.sample_rate = RATE
        self._window = vad.silero_vad.window_size
        self._vad = sherpa_onnx.VoiceActivityDetector(vad, buffer_size_in_seconds=120)
        self._heard = np.zeros(0, dtype=np.float32)  # not yet a whole window
        listen = models / LISTEN_DIR
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(listen / "encoder.int8.onnx"), decoder=str(listen / "decoder.int8.onnx"),
            joiner=str(listen / "joiner.int8.onnx"), tokens=str(listen / "tokens.txt"),
            num_threads=threads, model_type="nemo_transducer")
        speak = models / SPEAK_DIR
        tts = sherpa_onnx.OfflineTtsConfig()
        tts.model.kokoro.model = str(speak / "model.onnx")
        tts.model.kokoro.voices = str(speak / "voices.bin")
        tts.model.kokoro.tokens = str(speak / "tokens.txt")
        tts.model.kokoro.data_dir = str(speak / "espeak-ng-data")
        tts.model.kokoro.lexicon = str(speak / "lexicon-us-en.txt")
        tts.model.num_threads = threads
        self._tts = sherpa_onnx.OfflineTts(tts)

    def hear(self, samples: np.ndarray) -> list[np.ndarray]:
        self._heard = np.concatenate((self._heard, samples.astype(np.float32, copy=False)))
        while len(self._heard) >= self._window:
            self._vad.accept_waveform(self._heard[: self._window])
            self._heard = self._heard[self._window:]
        said = []
        while not self._vad.empty():
            said.append(np.array(self._vad.front.samples, dtype=np.float32))
            self._vad.pop()
        return said

    def reset(self) -> None:
        self._vad.reset()
        self._heard = np.zeros(0, dtype=np.float32)

    def transcribe(self, samples: np.ndarray) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(RATE, samples)
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()

    def speak(self, text: str) -> tuple[np.ndarray, int]:
        audio = self._tts.generate(text, sid=VOICE, speed=1.0)
        return np.asarray(audio.samples, dtype=np.float32), audio.sample_rate
