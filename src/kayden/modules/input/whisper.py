from datetime import datetime, timedelta, timezone
from queue import Queue, Empty
from time import sleep
import os
import threading
import numpy as np
import speech_recognition as sr
import whisper
from kayden.interfaces import InputInterface
from kayden.klogger import get_logger
from kayden.audio import resolve_pyaudio_input_device


def _cuda_available():
    try:
        import torch
        return torch.cuda.is_available()
    except (ImportError, RuntimeError):
        return False


class Module(InputInterface):
    def __init__(
        self,
        model=None,
        energy_threshold=1000,
        record_timeout=1.5,
        phrase_timeout=2.0,
        device=None,
        min_seconds=0.35,
        min_rms=0.006,
        min_avg_logprob=-0.8,
        max_no_speech_prob=0.6,
        max_compression_ratio=2.4,
    ):
        self.log = get_logger("Input")
        self.energy_threshold = energy_threshold
        self.record_timeout = record_timeout
        self.phrase_timeout = phrase_timeout
        self.min_seconds = float(min_seconds)
        self.min_rms = float(min_rms)
        self.min_avg_logprob = float(min_avg_logprob)
        self.max_no_speech_prob = float(max_no_speech_prob)
        self.max_compression_ratio = float(max_compression_ratio)

        self._cuda = _cuda_available()
        self.log.info("CUDA available: %s", self._cuda)

        device_index = resolve_pyaudio_input_device(device=device)
        self.log.info("Using input device index: %s", device_index)
        self.source = sr.Microphone(sample_rate=16000, device_index=device_index)
        self.recorder = self._setup_recorder()

        model = model or os.environ.get("KAYDEN_WHISPER_MODEL", "base.en")
        self.log.info("Loading Whisper model: %s", model)
        self.model = whisper.load_model(model)

        self.data_queue = Queue()
        self.output_queue = Queue()
        self.buffer = b""
        self.last_audio_time = None
        self.stop_listening = None

        self._running = False
        self._stopped = False
        self._thread = None
        self._start_lock = threading.Lock()

    def _setup_recorder(self):
        recorder = sr.Recognizer()
        recorder.energy_threshold = self.energy_threshold
        recorder.dynamic_energy_threshold = False
        # Ambient-noise calibration itself happens in _run_loop, not here —
        # see the note there for why.
        return recorder

    def _record_callback(self, _, audio: sr.AudioData):
        self.data_queue.put(audio.get_raw_data())

    def _drain_audio_queue(self) -> bytes:
        chunks = []
        while True:
            try:
                chunks.append(self.data_queue.get_nowait())
            except Empty:
                break
        return b"".join(chunks)

    def _audio_to_numpy(self, audio_bytes):
        return (
            np.frombuffer(audio_bytes, dtype=np.int16)
            .astype(np.float32) / 32768.0
        )

    def _usable_audio(self, audio):
        if audio.size / 16000 < self.min_seconds:
            return False
        rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
        return rms >= self.min_rms

    def _usable_segment(self, segment):
        text = segment.get("text", "").strip()
        return bool(text) and all((
            segment.get("avg_logprob", 0.0) >= self.min_avg_logprob,
            segment.get("no_speech_prob", 0.0) <= self.max_no_speech_prob,
            segment.get("compression_ratio", 0.0) <= self.max_compression_ratio,
        ))

    def _transcribe(self, audio_bytes):
        audio_np = self._audio_to_numpy(audio_bytes)
        if not self._usable_audio(audio_np):
            self.log.debug("dropped short or low-energy audio")
            return ""
        result = self.model.transcribe(
            audio_np,
            language="en",
            task="transcribe",
            fp16=self._cuda,
            temperature=0.0,
            condition_on_previous_text=False,
            compression_ratio_threshold=self.max_compression_ratio,
            logprob_threshold=-1.0,
            no_speech_threshold=self.max_no_speech_prob,
            word_timestamps=True,
            hallucination_silence_threshold=1.0,
        )
        segments = result.get("segments", [])
        text_parts = [segment["text"].strip() for segment in segments
                      if self._usable_segment(segment)]
        rejected = [segment.get("text", "") for segment in segments
                    if not self._usable_segment(segment)]
        if rejected:
            self.log.debug("dropped likely hallucinations: %s", rejected)

        return " ".join(text_parts).strip()

    def _get_combined_text(self):
        texts = []
        while True:
            try:
                texts.append(self.output_queue.get_nowait())
            except Empty:
                break
        return " ".join(texts) if texts else None

    def _process_audio(self):
        now = datetime.now(timezone.utc)
        audio_data = self._drain_audio_queue()
        if audio_data:
            self.buffer += audio_data
            self.last_audio_time = now
        if (
            self.buffer
            and self.last_audio_time
            and now - self.last_audio_time > timedelta(seconds=self.phrase_timeout)
        ):
            text = self._transcribe(self.buffer)
            if text:
                self.log.info("transcribed: %s", text)
                self.output_queue.put(text)
            self.buffer = b""
            self.last_audio_time = None

    def _run_loop(self):
        # Calibration and listen_in_background both open the mic's
        # PyAudio stream — doing that open/close/reopen sequence here,
        # on this single thread, is safe. What crashed the process
        # before was doing the open/close half in __init__ on the main
        # thread and the reopen half here on this background thread;
        # PortAudio's ALSA backend isn't safe to re-init across threads.
        with self.source:
            self.recorder.adjust_for_ambient_noise(self.source)
        self.log.info("calibrated energy_threshold: %s", self.recorder.energy_threshold)

        self.stop_listening = self.recorder.listen_in_background(
            self.source,
            self._record_callback,
            phrase_time_limit=self.record_timeout,
        )
        while self._running:
            self._process_audio()
            sleep(0.05)

    def _ensure_started(self):
        with self._start_lock:
            if self._stopped:
                raise EOFError("speech input stopped")
            if self._thread is not None:
                return
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()

    def stop(self):
        self.log.info("stopping")
        self._stopped = True
        self._running = False
        if self.stop_listening:
            self.stop_listening(wait_for_stop=False)
        if self._thread:
            self._thread.join()

    def get_input(self):
        self._ensure_started()
        while self._running:
            text = self._get_combined_text()
            if text:
                return text
            sleep(0.05)
        raise EOFError("speech input stopped")

    def has_input(self) -> bool:
        return not self.output_queue.empty()
