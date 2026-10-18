from dataclasses import dataclass


@dataclass(frozen=True)
class TurnRequest:
    messages: list[dict]


@dataclass(frozen=True)
class CoreTextDelta:
    text: str


@dataclass(frozen=True)
class CoreReasoningDelta:
    text: str


@dataclass(frozen=True)
class CoreToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class CoreUsage:
    usage: dict


CoreEvent = CoreTextDelta | CoreReasoningDelta | CoreToolCall | CoreUsage


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolStarted:
    call_id: str
    name: str
    arguments: dict
    description: str | None = None


@dataclass(frozen=True)
class ToolFinished:
    call_id: str
    name: str
    result: ToolResult
    description: str | None = None


@dataclass(frozen=True)
class UsageReported:
    usage: dict


ActionEvent = TextDelta | ToolStarted | ToolFinished | UsageReported
