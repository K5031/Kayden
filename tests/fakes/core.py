"""Scripted Core: each stream() call consumes the next Step (or calls a step factory)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from kayden.api import (
    JSON,
    Completed,
    Core,
    CoreError,
    CoreEvent,
    InferenceRequest,
    Message,
    ModelInfo,
    Reasoning,
    ReasoningDelta,
    Role,
    ServerToolUse,
    StopReason,
    Text,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolSpec,
    Usage,
    UsageReport,
)
from kayden.runtime.settings import read_key

DEFAULT_MODELS = (
    ModelInfo(
        "fake-1",
        "Fake One",
        context_window=100_000,
        max_output_tokens=1_000,
        input_usd_per_mtok=1.0,
        output_usd_per_mtok=2.0,
        cached_input_usd_per_mtok=0.5,
    ),
    ModelInfo("fake-2", "Fake Two", context_window=50_000, max_output_tokens=1_000),
)


def call(name: str, args: JSON | None = None, *, id: str | None = None) -> ToolCall:
    return ToolCall(id or f"call_{uuid.uuid4().hex[:8]}", name, args or {})


@dataclass
class Step:
    """One scripted model response. Deltas first, then reports, then error/hang, then
    Completed."""

    text: str = ""
    reasoning: str = ""
    calls: Sequence[ToolCall] = ()
    server: Sequence[ServerToolUse] = ()  # tools the provider ran, before the text
    usage: Usage | None = None  # default Usage(100, 10)
    stop: StopReason | None = None  # default: TOOL_USE if calls else END_TURN
    error: BaseException | None = None
    reports: Sequence[Usage] = ()
    chunks: int = 1
    delay: float = 0.0  # before each delta
    hang: bool = False  # wait forever after deltas (cancellable)
    ignore_cancel: asyncio.Event | None = None  # with hang: swallow cancellation until set


type StepSource = Step | Callable[[InferenceRequest], Step]


class ScriptedCore(Core):
    def __init__(
        self,
        steps: Sequence[StepSource] = (),
        *,
        models: Sequence[ModelInfo] = DEFAULT_MODELS,
        default: StepSource | None = None,
        maintenance: StepSource | None = None,
        trace: list[Any] | None = None,
    ):
        self.steps: deque[StepSource] = deque(steps)
        self.maintenance = maintenance  # answers maintenance requests without using steps
        self.models = list(models)
        self.default = default or Step(text="ok")
        self.requests: list[InferenceRequest] = []
        self.active = 0
        self.max_active = 0
        self.cancelled = 0
        self.trace = trace if trace is not None else []
        self.count_tokens_result: int | None = None
        self.options: dict[str, Any] = {}
        self.list_error: Exception | None = None
        self.closed = False
        self.tools: list[ToolSpec] = []  # tools the provider runs itself (list_tools)

    def add(self, *steps: StepSource) -> None:
        self.steps.extend(steps)

    async def list_models(self) -> Sequence[ModelInfo]:
        if self.list_error is not None:
            raise self.list_error
        return self.models

    async def aclose(self) -> None:
        self.closed = True

    async def list_tools(self) -> Sequence[ToolSpec]:
        return self.tools

    async def count_tokens(self, request: InferenceRequest) -> int | None:
        return self.count_tokens_result

    async def stream(self, request: InferenceRequest) -> AsyncIterator[CoreEvent]:
        self.requests.append(request)
        if request.maintenance and self.maintenance is not None:
            source = self.maintenance
        else:
            source = self.steps.popleft() if self.steps else self.default
        step = source(request) if callable(source) else source
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.trace.append(("infer_start", request.model))
        try:
            async for event in self._events(request, step):
                yield event
        except (asyncio.CancelledError, GeneratorExit):  # task cancelled or stream closed
            self.cancelled += 1
            raise
        finally:
            self.active -= 1
            self.trace.append(("infer_end", request.model))

    async def _events(self, request: InferenceRequest, step: Step) -> AsyncIterator[CoreEvent]:
        if step.reasoning:
            await asyncio.sleep(step.delay)
            yield ReasoningDelta(step.reasoning)
        if step.text:
            size = max(1, -(-len(step.text) // step.chunks))
            for i in range(0, len(step.text), size):
                await asyncio.sleep(step.delay)
                yield TextDelta(step.text[i : i + size])
        for c in step.calls:
            await asyncio.sleep(step.delay)
            yield ToolCallDelta(c.id, c.name, c.raw_arguments or json.dumps(c.arguments))
        for report in step.reports:
            await asyncio.sleep(step.delay)
            yield UsageReport(report)
        if step.error is not None:
            raise step.error
        if step.hang:
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    if step.ignore_cancel is None:
                        raise
                    await step.ignore_cancel.wait()
                    raise
        blocks: list[Any] = []
        if step.reasoning:
            blocks.append(Reasoning(step.reasoning, model=request.model))
        blocks.extend(step.server)
        if step.text:
            blocks.append(Text(step.text))
        blocks.extend(step.calls)
        stop = step.stop or (StopReason.TOOL_USE if step.calls else StopReason.END_TURN)
        message = Message(uuid.uuid4().hex, Role.ASSISTANT, tuple(blocks), 0.0)
        yield Completed(message, step.usage or Usage(100, 10), stop)


def factory(options: dict[str, Any], paths: Any) -> ScriptedCore:
    """Options: steps, default, maintenance, models (ids; each gets a 100k window and
    prices), and
    list_error (a message: list_models raises CoreError, like a rejected key)."""
    models = DEFAULT_MODELS
    if "models" in options:
        models = tuple(ModelInfo(m, m, context_window=100_000, max_output_tokens=1_000,
                                 input_usd_per_mtok=1.0, output_usd_per_mtok=2.0)
                       for m in options["models"])
    core = ScriptedCore(options.get("steps", ()), models=models, default=options.get("default"),
                        maintenance=options.get("maintenance"))
    core.options = dict(options)
    if "list_error" in options:
        core.list_error = CoreError(options["list_error"])
    return core


def first_factory(options: dict[str, Any], paths: Any) -> ScriptedCore:
    """An installed Core that needs FIRST_API_KEY (registered by the `installed` fixture)."""
    if not read_key("FIRST_API_KEY"):
        raise ValueError("FIRST_API_KEY is not set")
    core = factory(options, paths)
    core.options["key"] = read_key("FIRST_API_KEY")
    return core


def second_factory(options: dict[str, Any], paths: Any) -> ScriptedCore:
    return factory({"models": ["s-1", "s-2"], **options}, paths)


first_factory.display_name = "First AI"  # type: ignore[attr-defined]
first_factory.key_name = "FIRST_API_KEY"  # type: ignore[attr-defined]
second_factory.display_name = "Second AI"  # type: ignore[attr-defined]
second_factory.key_name = "SECOND_API_KEY"  # type: ignore[attr-defined]
