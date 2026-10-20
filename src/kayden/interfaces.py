from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Iterator

from kayden.events import (
    ActionEvent,
    CoreEvent,
    ToolResult,
    TurnRequest,
)
from kayden.frontend_api import FrontendAPI


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
    """Interactive presentation layer connected through ``FrontendAPI``."""

    @abstractmethod
    def run(
        self,
        api: FrontendAPI,
    ) -> None:
        """Subscribe to events, send commands, and run until the UI closes."""


class CoreInterface(ABC):
    runtime_kind: str | None = None
    reports_usage: bool = False

    def available_models(self) -> list[dict[str, str]]:
        """Return models that can be selected without replacing the core."""
        model = getattr(self, "model", None)
        if not isinstance(model, str) or not model:
            return []
        display_name = getattr(self, "model_display_name", model)
        return [{"id": model, "name": display_name}]

    def select_model(self, model: str) -> None:
        """Select a model for subsequent turns when the core supports it."""
        if model != getattr(self, "model", None):
            raise ValueError(f"model '{model}' is unavailable")

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
    summary_target_tokens: int | None = None

    @abstractmethod
    def prepare(self, view: MemoryView) -> list[dict]:
        """Select and format stored conversation for the next model turn."""
        pass

    def needs_compaction(self, view: MemoryView) -> bool:
        return False

    def retained_message_count(self, view: MemoryView) -> int:
        """Return how many newest transcript messages should remain verbatim."""
        return min(len(view.messages), max(2, int(self.keep_recent_messages)))

    def constrain_summary(self, summary: str) -> str:
        """Bound a generated summary to this policy's context budget."""
        return summary.strip()

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

    def describe_activity(
        self,
        name: str,
        arguments: dict,
        state: str,
        result: ToolResult | None = None,
    ) -> str | None:
        """Optionally describe a call for presentation by any frontend."""
        return None


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

    def describe_activity(
        self,
        name: str,
        arguments: dict,
        state: str,
        result: ToolResult | None = None,
    ) -> str:
        """Return a concise provider-owned description of tool activity."""
        label = " ".join(name.split("_")).capitalize() or "Tool"
        if state == "running":
            return f"Running {label}…"
        if state == "failed":
            return f"{label} failed"
        return f"Finished {label}"


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
