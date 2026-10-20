"""Stable, transport-neutral contract between Kayden and its frontends."""

from dataclasses import dataclass
import threading
from typing import Callable

from kayden.events import ToolResult


# Commands travel from a frontend to the application runtime.


@dataclass(frozen=True)
class SubmitMessage:
    text: str


@dataclass(frozen=True)
class InterruptTurn:
    pass


@dataclass(frozen=True)
class SelectModel:
    model: str


@dataclass(frozen=True)
class ApplyModuleSelections:
    selections: dict


@dataclass(frozen=True)
class StartCapture:
    pass


@dataclass(frozen=True)
class FinishCapture:
    pass


@dataclass(frozen=True)
class CancelCapture:
    pass


@dataclass(frozen=True)
class InvokeControl:
    control_id: str
    value: object = None


# Events travel from the application runtime to a frontend.


@dataclass(frozen=True)
class IdentityConfigured:
    assistant_name: str


@dataclass(frozen=True)
class ModelsConfigured:
    models: list[dict[str, str]]
    selected_model: str


@dataclass(frozen=True)
class ModulesConfigured:
    modules: list[dict]


@dataclass(frozen=True)
class CaptureConfigured:
    available: bool
    label: str = "Capture"


@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class AssistantText:
    text: str


@dataclass(frozen=True)
class StatusChanged:
    text: str


@dataclass(frozen=True)
class ToolActivityStarted:
    call_id: str
    name: str
    arguments: dict
    description: str | None = None


@dataclass(frozen=True)
class ToolActivityFinished:
    call_id: str
    name: str
    result: ToolResult
    description: str | None = None


@dataclass(frozen=True)
class TurnFinished:
    interrupted: bool = False


@dataclass(frozen=True)
class CaptureStateChanged:
    state: str


@dataclass(frozen=True)
class DraftChanged:
    text: str


@dataclass(frozen=True)
class ControlsConfigured:
    controls: list[dict]


FrontendCommand = (
    SubmitMessage
    | InterruptTurn
    | SelectModel
    | ApplyModuleSelections
    | StartCapture
    | FinishCapture
    | CancelCapture
    | InvokeControl
)

FrontendEvent = (
    IdentityConfigured
    | ModelsConfigured
    | ModulesConfigured
    | CaptureConfigured
    | UserMessage
    | AssistantText
    | StatusChanged
    | ToolActivityStarted
    | ToolActivityFinished
    | TurnFinished
    | CaptureStateChanged
    | DraftChanged
    | ControlsConfigured
)


class FrontendAPI:
    """In-process frontend connection with a transport-friendly shape.

    Frontends can only send typed commands and subscribe to typed events. The
    runtime keeps the publishing side private. Events produced before the UI
    subscribes are replayed once, which lets configuration and transcript
    restoration happen before a platform event loop starts.
    """

    def __init__(self, command_handler: Callable[[FrontendCommand], None]):
        self._command_handler = command_handler
        self._listener = None
        self._pending = []
        self._lock = threading.RLock()
        self._closed = False

    def send(self, command: FrontendCommand) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("frontend connection is closed")
        self._command_handler(command)

    def subscribe(
        self,
        listener: Callable[[FrontendEvent], None],
    ) -> Callable[[], None]:
        if not callable(listener):
            raise TypeError("frontend event listener must be callable")
        with self._lock:
            if self._closed:
                raise RuntimeError("frontend connection is closed")
            if self._listener is not None:
                raise RuntimeError("frontend connection already has a subscriber")
            self._listener = listener
            pending = self._pending
            self._pending = []
        for event in pending:
            listener(event)

        def unsubscribe():
            with self._lock:
                if self._listener is listener:
                    self._listener = None

        return unsubscribe

    def _publish(self, event: FrontendEvent) -> None:
        with self._lock:
            if self._closed:
                return
            listener = self._listener
            if listener is None:
                self._pending.append(event)
                return
        listener(event)

    def _close(self) -> None:
        with self._lock:
            self._closed = True
            self._listener = None
            self._pending = []
