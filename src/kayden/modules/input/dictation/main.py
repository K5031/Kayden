"""Push-to-talk dictation module entrypoint."""

import os
import threading

from kayden.interfaces import ManualInputInterface
from kayden.klogger import get_logger

from .capture import AudioCapture
from .transcriber import FasterWhisperTranscriber


class Module(ManualInputInterface):
    """Push-to-talk dictation backed by Faster Whisper and Silero VAD."""

    capture_label = "Mic"

    def __init__(self, capture=None, transcriber=None):
        self.log = get_logger("Dictation")
        self._capture = capture or AudioCapture()
        self._transcriber = transcriber
        self._transcriber_lock = threading.Lock()

    def _load_transcriber(self):
        with self._transcriber_lock:
            if self._transcriber is None:
                device = os.environ.get("KAYDEN_DICTATION_DEVICE", "cpu")
                default_compute_type = "int8" if device.casefold() == "cpu" else "default"
                self._transcriber = FasterWhisperTranscriber(
                    model=os.environ.get("KAYDEN_DICTATION_MODEL", "small.en"),
                    device=device,
                    compute_type=os.environ.get(
                        "KAYDEN_DICTATION_COMPUTE_TYPE", default_compute_type
                    ),
                )
            return self._transcriber

    def start_capture(self) -> None:
        self._capture.start()

    def finish_capture(self) -> str:
        audio = self._capture.finish()
        if not audio:
            return ""
        text = self._load_transcriber().transcribe(audio)
        if text:
            self.log.info("transcribed: %s", text)
        return text

    def cancel_capture(self) -> None:
        self._capture.cancel()

    def stop(self) -> None:
        self.cancel_capture()
