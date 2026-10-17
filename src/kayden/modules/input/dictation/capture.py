import threading

from kayden.audio import resolve_pyaudio_input_device


class AudioCapture:
    """Collect 16 kHz mono PCM while a push-to-talk control is held open."""

    def __init__(self, device=None, sample_rate=16000, chunk_size=1024, max_seconds=120):
        self.device = device
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.max_bytes = sample_rate * 2 * max_seconds
        self._lock = threading.Lock()
        self._audio = None
        self._stream = None
        self._chunks = []
        self._byte_count = 0
        self._recording = False

    @property
    def recording(self):
        with self._lock:
            return self._recording

    def _callback(self, data, frame_count, time_info, status_flags):
        import pyaudio

        with self._lock:
            if not self._recording:
                return (None, pyaudio.paComplete)
            remaining = self.max_bytes - self._byte_count
            if remaining > 0:
                chunk = data[:remaining]
                self._chunks.append(chunk)
                self._byte_count += len(chunk)
            complete = remaining <= len(data)
        return (None, pyaudio.paComplete if complete else pyaudio.paContinue)

    def start(self):
        import pyaudio

        with self._lock:
            if self._recording:
                return

        device_index = resolve_pyaudio_input_device(device=self.device)
        audio = pyaudio.PyAudio()
        try:
            stream = audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=self.chunk_size,
                stream_callback=self._callback,
                start=False,
            )
        except Exception:
            audio.terminate()
            raise

        with self._lock:
            self._audio = audio
            self._stream = stream
            self._chunks = []
            self._byte_count = 0
            self._recording = True
        try:
            stream.start_stream()
        except Exception:
            self._stop(discard=True)
            raise

    def _stop(self, discard):
        with self._lock:
            stream = self._stream
            audio = self._audio
            chunks = self._chunks
            self._stream = None
            self._audio = None
            self._chunks = []
            self._byte_count = 0
            self._recording = False

        try:
            if stream is not None:
                if stream.is_active():
                    stream.stop_stream()
                stream.close()
        finally:
            if audio is not None:
                audio.terminate()
        return b"" if discard else b"".join(chunks)

    def finish(self):
        return self._stop(discard=False)

    def cancel(self):
        self._stop(discard=True)

