import threading
import queue
import os
import html
import re
from queue import Empty
from kayden.interfaces import OutputInterface
from kayden.klogger import get_logger
from kayden.audio import ensure_audio_ready, resolve_output_device


def _load_dependencies():
    """Import the optional speech stack only when this module is selected."""
    import sounddevice as sd
    from kokoro import KPipeline

    return sd, KPipeline


def _text_for_speech(text: str) -> str:
    """Turn model Markdown into text suitable for a speech synthesizer."""
    text = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"<https?://[^>]+>", "", text)
    text = re.sub(r"(?m)^\s{0,3}(?:#{1,6}|>|[-+*]|\d+[.)])\s+", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("```", "").replace("`", "")
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    text = text.replace("*", "")
    text = re.sub(r"\\([\\`*{}\[\]()#+.!_>~-])", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _ends_sentence(text: str) -> bool:
    return bool(re.search(r"[.!?][\"')\]}*_~`]*\s*$", text))


class Module(OutputInterface):
    def __init__(self, voice=None, lang_code=None, repo_id=None, device=None):
        self.log = get_logger("Kokoro")
        self.stream = None
        self.thread = None
        self.running = False
        self._stopped = False
        self._start_lock = threading.Lock()
        self._stream_close_lock = threading.Lock()

        self.log.info("Loading Kokoro dependencies")
        self._sounddevice, pipeline_factory = _load_dependencies()
        ensure_audio_ready()

        self.device_index = resolve_output_device(device=device)
        self.log.info("Using output device index: %s", self.device_index)
        voice = voice or os.environ.get("KAYDEN_KOKORO_VOICE", "af_heart")
        lang_code = lang_code or os.environ.get("KAYDEN_KOKORO_LANGUAGE", "a")
        repo_id = repo_id or os.environ.get("KAYDEN_KOKORO_REPO", "hexgrad/Kokoro-82M")
        pipeline_device = os.environ.get("KAYDEN_KOKORO_DEVICE")
        self.log.info("Loading pipeline: %s (lang=%s, voice=%s)", repo_id, lang_code, voice)
        self.pipeline = pipeline_factory(
            lang_code=lang_code,
            repo_id=repo_id,
            device=pipeline_device,
        )
        self.voice = voice
        self.buffer = ""
        self.queue = queue.Queue()
        self._generation = 0

    def _ensure_started(self):
        with self._start_lock:
            if self._stopped:
                raise RuntimeError("speech output stopped")
            if self.running:
                return
            try:
                self.stream = self._sounddevice.OutputStream(
                    samplerate=24000,
                    channels=1,
                    dtype="float32",
                    device=self.device_index,
                )
                self.stream.start()
                self.running = True
                self.thread = threading.Thread(target=self._worker, daemon=True)
                self.thread.start()
            except Exception:
                self._close_stream()
                raise

    def _worker(self):
        try:
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
        finally:
            self.running = False
            self._close_stream()

    def _close_stream(self):
        """Close the native stream once, preferably from its writer thread."""
        with self._stream_close_lock:
            stream = self.stream
            self.stream = None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception as error:
            self.log.debug("audio stream stop failed: %s", error)
        try:
            stream.close()
        except Exception as error:
            self.log.debug("audio stream close failed: %s", error)

    def send(self, token: str):
        self.buffer += token
        if _ends_sentence(self.buffer):
            self._queue_buffer()

    def flush(self):
        if self.buffer.strip():
            self.log.debug("flushing trailing text: %r", self.buffer)
            self._queue_buffer()

    def _queue_buffer(self):
        text = _text_for_speech(self.buffer)
        self.buffer = ""
        if not text:
            return
        self._ensure_started()
        self.queue.put((self._generation, text))

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
        self.interrupt()
        self._stopped = True
        self.running = False
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=0.1)
        if self.thread is None or not self.thread.is_alive():
            self._close_stream()
        else:
            self.log.debug("speech worker is still unwinding; deferring stream cleanup")
