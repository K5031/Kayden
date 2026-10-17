import threading
import queue
import os
import sounddevice as sd
from kokoro import KPipeline
from queue import Empty
from kayden.interfaces import OutputInterface
from kayden.klogger import get_logger
from kayden.audio import ensure_audio_ready, resolve_output_device


class Module(OutputInterface):
    def __init__(self, voice=None, lang_code=None, repo_id=None, device=None):
        self.log = get_logger("Kokoro")
        self.stream = None
        self.thread = None
        self.running = False

        ensure_audio_ready()

        try:
            device_index = resolve_output_device(device=device)
            self.log.info("Using output device index: %s", device_index)
            voice = voice or os.environ.get("KAYDEN_KOKORO_VOICE", "af_heart")
            lang_code = lang_code or os.environ.get("KAYDEN_KOKORO_LANGUAGE", "a")
            repo_id = repo_id or os.environ.get("KAYDEN_KOKORO_REPO", "hexgrad/Kokoro-82M")
            pipeline_device = os.environ.get("KAYDEN_KOKORO_DEVICE")
            self.log.info("Loading pipeline: %s (lang=%s, voice=%s)", repo_id, lang_code, voice)
            self.pipeline = KPipeline(
                lang_code=lang_code,
                repo_id=repo_id,
                device=pipeline_device,
            )
            self.voice = voice
            self.buffer = ""
            self.queue = queue.Queue()
            self._generation = 0

            self.stream = sd.OutputStream(
                samplerate=24000,
                channels=1,
                dtype="float32",
                device=device_index,
            )
            self.stream.start()

            self.running = True
            self.thread = threading.Thread(target=self._worker, daemon=True)
            self.thread.start()
        except Exception:
            if self.stream is not None:
                self.stream.close()
            raise

    def _worker(self):
        while self.running:
            try:
                gen, text = self.queue.get(timeout=0.2)
            except Empty:
                continue

            if gen != self._generation:
                continue

            try:
                for _, _, audio in self.pipeline(text, voice=self.voice):
                    if not self.running or gen != self._generation:
                        break
                    self.stream.write(audio.reshape(-1, 1))
            except Exception as e:
                self.log.error("TTS synthesis failed for %r: %s", text, e)

    def send(self, token: str):
        self.buffer += token
        if self.buffer.endswith((".", "!", "?")):
            self.queue.put((self._generation, self.buffer))
            self.buffer = ""

    def flush(self):
        if self.buffer.strip():
            self.log.debug("flushing trailing text: %r", self.buffer)
            self.queue.put((self._generation, self.buffer))
        self.buffer = ""

    def interrupt(self):
        self.log.debug("interrupted")
        self._generation += 1
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
        self.buffer = ""

    def stop(self):
        self.log.info("stopping")
        self.running = False
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=2)
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
