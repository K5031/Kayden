from abc import ABC, abstractmethod
from typing import Callable, Iterator

from events import ActionEvent, CoreEvent, ToolResult, TurnRequest


class InputInterface(ABC):
    @abstractmethod
    def get_input(self) -> str:
        pass

    @abstractmethod
    def has_input(self) -> bool:
        pass


class OutputInterface(ABC):
    @abstractmethod
    def send(self, token: str) -> None:
        pass

    @abstractmethod
    def interrupt(self) -> None:
        pass

    @abstractmethod
    def flush(self) -> None:
        pass


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


class ContextInterface(ABC):
    usage_mode: str | None = None

    @abstractmethod
    def add(self, role: str, content: str) -> None:
        pass

    @abstractmethod
    def get(self) -> list[dict]:
        pass

    @abstractmethod
    def clear(self) -> None:
        pass


class MemoryInterface(ABC):
    @abstractmethod
    def store(self, messages: list[dict]) -> None:
        pass

    @abstractmethod
    def retrieve(self, query: str) -> str:
        pass


class ToolInterface(ABC):
    @abstractmethod
    def schemas(self) -> list[dict]:
        """Return the function definitions exposed by this tool module."""

    @abstractmethod
    def execute(self, name: str, arguments: dict) -> str:
        """Validate and run a named tool, returning text for the model."""


class ToolRuntimeInterface(ABC):
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


class ControllerInterface(ABC):
    required_modules: list[str] = None
    optional_modules: list[str] = []
    required_features: dict[str, dict[str, object]] = {}

    def __init_subclass__(cls):
        super().__init_subclass__()
        if cls.required_modules is None:
            raise TypeError(f"{cls.__name__} must define required_modules")

    @abstractmethod
    def run(self, api) -> None:
        pass
