import selectors
import sys
import threading
import queue
from kayden.interfaces import InputInterface


class Module(InputInterface):
    def __init__(self):
        self.output_queue = queue.Queue()
        self.running = False
        self._stopped = False
        self._selector = None
        self.thread = None
        self._start_lock = threading.Lock()

    def _ensure_started(self):
        with self._start_lock:
            if self._stopped:
                raise EOFError("stdin input stopped")
            if self.thread is not None:
                return
            self._selector = selectors.DefaultSelector()
            self._selector.register(sys.stdin, selectors.EVENT_READ)
            self.running = True
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()

    def _run_loop(self):
        while self.running:
            events = self._selector.select(timeout=0.2)
            if not events:
                continue
            line = sys.stdin.readline()
            if line == "":
                self.output_queue.put(None)
                break
            text = line.rstrip("\n")
            self.output_queue.put(text)

    def get_input(self) -> str:
        self._ensure_started()
        print("\n> ", end="", flush=True)
        value = self.output_queue.get()
        if value is None:
            raise EOFError("stdin closed")
        return value

    def has_input(self) -> bool:
        return not self.output_queue.empty()

    def stop(self):
        self._stopped = True
        self.running = False
        self.output_queue.put(None)
        if self.thread:
            self.thread.join(timeout=1)
        if self._selector:
            self._selector.close()
