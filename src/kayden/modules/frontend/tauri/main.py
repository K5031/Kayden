"""Tauri frontend module entrypoint."""

import os
from typing import Callable

from kayden.interfaces import FrontendInterface
from .bridge import DesktopFrontend


_WINDOWS = {
    "gui": {
        "title": "Kayden",
        "width": 1000,
        "height": 760,
        "floating": False,
    },
    "floaty": {
        "title": "Kayden · Floaty",
        "width": 560,
        "height": 440,
        "floating": True,
    },
}


class Module(FrontendInterface):
    """Installable React/Tauri frontend with full and floating layouts."""

    def __init__(self, mode="gui"):
        mode = os.environ.get("KAYDEN_TAURI_MODE", mode).lower()
        try:
            window = _WINDOWS[mode]
        except KeyError as error:
            raise ValueError(
                "KAYDEN_TAURI_MODE must be either 'gui' or 'floaty'"
            ) from error
        self._desktop = DesktopFrontend(mode=mode, **window)

    def __getattr__(self, name):
        return getattr(self._desktop, name)

    def configure_identity(self, assistant_name: str) -> None:
        self._desktop.configure_identity(assistant_name)

    def configure_models(self, models, selected_model, on_select) -> bool:
        return self._desktop.configure_models(models, selected_model, on_select)

    def configure_capture(
        self,
        on_start: Callable[[], None],
        on_finish: Callable[[], None],
        on_cancel: Callable[[], None],
        label: str = "Capture",
    ) -> bool:
        return self._desktop.configure_capture(
            on_start, on_finish, on_cancel, label
        )

    def user(self, message: str) -> None:
        self._desktop.user(message)

    def assistant(self, token: str) -> None:
        self._desktop.assistant(token)

    def status(self, message: str) -> None:
        self._desktop.status(message)

    def finish(self, interrupted: bool = False) -> None:
        self._desktop.finish(interrupted)

    def capture_state(self, state: str) -> None:
        self._desktop.capture_state(state)

    def draft(self, text: str) -> None:
        self._desktop.draft(text)

    def run(
        self,
        on_submit: Callable[[str], None],
        on_interrupt: Callable[[], None],
    ) -> None:
        self._desktop.run(on_submit, on_interrupt)

    def stop(self) -> None:
        self._desktop.stop()
