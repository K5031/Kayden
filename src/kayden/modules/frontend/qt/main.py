"""Qt frontend module entrypoint."""

import queue
import threading
from typing import Callable

from kayden.interfaces import FrontendInterface


class Module(FrontendInterface):
    """Native Qt Quick frontend backed by PySide6 and QML."""

    def __init__(self, width=1000, height=760):
        if not isinstance(width, int) or width < 480:
            raise ValueError("qt frontend width must be an integer of at least 480")
        if not isinstance(height, int) or height < 420:
            raise ValueError("qt frontend height must be an integer of at least 420")
        self.width = width
        self.height = height
        self.assistant_name = "Kayden"
        self._events = queue.SimpleQueue()
        self._models = []
        self._selected_model = None
        self._on_model_select = None
        self._capture_label = "Capture"
        self._on_capture_start = None
        self._on_capture_finish = None
        self._on_capture_cancel = None
        self._on_submit = None
        self._on_interrupt = None
        self._closed = threading.Event()

    def configure_identity(self, assistant_name: str) -> None:
        self.assistant_name = assistant_name

    def configure_models(self, models, selected_model, on_select) -> bool:
        normalized = []
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            name = model.get("name")
            if not isinstance(model_id, str) or not model_id:
                continue
            normalized.append({
                "id": model_id,
                "name": name if isinstance(name, str) and name else model_id,
            })
        if not normalized or selected_model not in {
            model["id"] for model in normalized
        }:
            return False
        self._models = normalized
        self._selected_model = selected_model
        self._on_model_select = on_select
        return True

    def configure_capture(
        self,
        on_start: Callable[[], None],
        on_finish: Callable[[], None],
        on_cancel: Callable[[], None],
        label: str = "Capture",
    ) -> bool:
        self._on_capture_start = on_start
        self._on_capture_finish = on_finish
        self._on_capture_cancel = on_cancel
        self._capture_label = label
        return True

    def user(self, message: str) -> None:
        self._events.put(("user", message))

    def assistant(self, token: str) -> None:
        self._events.put(("assistant", token))

    def status(self, message: str) -> None:
        self._events.put(("status", message))

    def finish(self, interrupted: bool = False) -> None:
        self._events.put(("finish", interrupted))

    def capture_state(self, state: str) -> None:
        self._events.put(("capture_state", state))

    def draft(self, text: str) -> None:
        self._events.put(("draft", text))

    def _drain_events(self):
        while True:
            try:
                yield self._events.get_nowait()
            except queue.Empty:
                return

    def _submit(self, message: str) -> None:
        message = message.strip()
        if message and self._on_submit:
            self._on_submit(message)

    def _interrupt(self) -> None:
        if self._on_interrupt:
            self._on_interrupt()

    def _select_model(self, model: str) -> None:
        choices = {option["id"]: option["name"] for option in self._models}
        if model not in choices:
            raise ValueError("model is unavailable")
        if not self._on_model_select:
            raise ValueError("model selection is unavailable")
        self._on_model_select(model)
        self._selected_model = model
        self._events.put(("model", (model, choices[model])))

    def _capture(self, action: str) -> None:
        callbacks = {
            "start": self._on_capture_start,
            "finish": self._on_capture_finish,
            "cancel": self._on_capture_cancel,
        }
        callback = callbacks.get(action)
        if callback is None:
            raise ValueError("capture action is unavailable")
        callback()

    def _close_from_ui(self) -> None:
        if self._closed.is_set():
            return
        if self._on_capture_cancel:
            self._on_capture_cancel()
        self._interrupt()
        self._closed.set()

    def run(
        self,
        on_submit: Callable[[str], None],
        on_interrupt: Callable[[], None],
    ) -> None:
        self._on_submit = on_submit
        self._on_interrupt = on_interrupt
        self._closed.clear()
        try:
            from .backend import run_qt
        except ModuleNotFoundError as error:
            if error.name == "PySide6" or error.name.startswith("PySide6."):
                raise RuntimeError(
                    "The Qt frontend requires PySide6. Install it with "
                    "`pip install 'kayden[qt]'`."
                ) from error
            raise
        run_qt(self)

    def stop(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._events.put(("close", None))
