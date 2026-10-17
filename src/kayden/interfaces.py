from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Iterator

from kayden.events import (
    ActionEvent,
    CoreEvent,
    ToolResult,
    TurnRequest,
)


@dataclass(frozen=True)
class MemoryView:
    summary: str
    messages: list[dict]


@dataclass(frozen=True)
class CompactionPlan:
    previous_summary: str
    messages: list[dict]
    base_checkpoint: int
    through_checkpoint: int


class InputInterface(ABC):
    @abstractmethod
    def get_input(self) -> str:
        pass

    @abstractmethod
    def has_input(self) -> bool:
        pass


class ManualInputInterface(InputInterface):
    """Input collected explicitly by a frontend control such as push-to-talk."""

    capture_label = "Capture"

    def get_input(self) -> str:
        raise RuntimeError("manual input must be driven through capture controls")

    def has_input(self) -> bool:
        return False

    @abstractmethod
    def start_capture(self) -> None:
        """Begin collecting input without blocking the frontend event loop."""

    @abstractmethod
    def finish_capture(self) -> str:
        """Stop capture and return text suitable for the frontend draft."""

    @abstractmethod
    def cancel_capture(self) -> None:
        """Stop capture and discard the collected input."""


class OutputInterface(ABC):
    @abstractmethod
    def send(self, token: str) -> None:
        pass

    def status(self, message: str) -> None:
        """Display transient activity without treating it as assistant text."""

    @abstractmethod
    def interrupt(self) -> None:
        pass

    @abstractmethod
    def flush(self) -> None:
        pass


class FrontendInterface(ABC):
    """Interactive presentation layer for Kayden.

    ``run`` owns the platform event loop. The other methods may be called from
    the application's worker thread, so frontend implementations must marshal
    rendering back to their UI thread.
    """

    @abstractmethod
    def run(
        self,
        on_submit: Callable[[str], None],
        on_interrupt: Callable[[], None],
    ) -> None:
        """Run until the user closes the frontend."""

    @abstractmethod
    def user(self, message: str) -> None:
        """Render a submitted user message."""

    @abstractmethod
    def assistant(self, token: str) -> None:
        """Append streamed assistant text to the current turn."""

    @abstractmethod
    def status(self, message: str) -> None:
        """Render transient activity such as a tool call."""

    @abstractmethod
    def finish(self, interrupted: bool = False) -> None:
        """Mark the current assistant turn as complete."""

    def configure_capture(
        self,
        on_start: Callable[[], None],
        on_finish: Callable[[], None],
        on_cancel: Callable[[], None],
        label: str = "Capture",
    ) -> bool:
        """Attach optional manual-input controls; return whether they are supported."""
        return False

    def capture_state(self, state: str) -> None:
        """Render ``idle``, ``recording``, or ``transcribing`` capture state."""

    def draft(self, text: str) -> None:
        """Insert captured text into the editable composer for user review."""


class CoreInterface(ABC):
    runtime_kind: str | None = None
    reports_usage: bool = False

    @abstractmethod
    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        should_interrupt: Callable[[], bool] | None = None,
    ) -> Iterator[CoreEvent]:
        pass

    @abstractmethod
    def interrupt(self) -> None:
        pass


class ContextPolicyInterface(ABC):
    usage_mode: str | None = None
    keep_recent_messages: int = 12

    @abstractmethod
    def prepare(self, view: MemoryView) -> list[dict]:
        """Select and format stored conversation for the next model turn."""
        pass

    def needs_compaction(self, view: MemoryView) -> bool:
        return False

    def compacted(self, view: MemoryView) -> None:
        """Update token accounting after memory compacts its transcript."""

    def report_usage(self, prompt_tokens: int) -> None:
        """Accept provider-reported prompt usage when ``usage_mode`` requires it."""

    def reset(self) -> None:
        """Reset transient context accounting."""


class MemoryInterface(ABC):
    @abstractmethod
    def append(self, messages: list[dict]) -> None:
        """Record messages and perform any backend-specific learning."""
        pass

    @abstractmethod
    def recall(self, query: str) -> str:
        """Return durable information relevant to a new user query."""
        pass

    @abstractmethod
    def view(self) -> MemoryView:
        """Return the summary and active transcript as structured state."""

    def recent(self, limit: int = 50) -> list[dict]:
        return self.view().messages[-limit:]

    def plan_compaction(self, keep_recent: int) -> CompactionPlan | None:
        return None

    def commit_compaction(self, plan: CompactionPlan, summary: str) -> bool:
        return False

    def reset_context(self) -> None:
        """Start fresh active context without requiring history deletion."""

    def actions(self) -> "ToolInterface | None":
        return None


class ToolInterface(ABC):
    @abstractmethod
    def schemas(self) -> list[dict]:
        """Return the function definitions exposed by this tool module."""

    @abstractmethod
    def execute(self, name: str, arguments: dict) -> str:
        """Validate and run a named tool, returning text for the model."""


class ToolRuntimeInterface(ABC):
    @abstractmethod
    def add(self, module: ToolInterface) -> None:
        """Expose tools from a borrowed capability module."""

    @abstractmethod
    def schemas(self) -> list[dict]:
        pass

    @abstractmethod
    def execute(self, name: str, arguments: dict) -> ToolResult:
        pass


class ActionInterface(ABC):
    @abstractmethod
    def run(
        self,
        core: CoreInterface,
        turn: TurnRequest,
        tools: ToolRuntimeInterface,
        should_interrupt: Callable[[], bool] | None = None,
    ) -> Iterator[ActionEvent]:
        pass
