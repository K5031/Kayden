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
    CoreEvent,
    InferenceRequest,
    Message,
    ModelInfo,
    Reasoning,
    ReasoningDelta,
    Role,
    StopReason,
    Text,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    Usage,
    UsageReport,
)

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
        trace: list[Any] | None = None,
    ):
        self.steps: deque[StepSource] = deque(steps)
        self.models = list(models)
        self.default = default or Step(text="ok")
        self.requests: list[InferenceRequest] = []
        self.active = 0
        self.max_active = 0
        self.cancelled = 0
        self.trace = trace if trace is not None else []
        self.count_tokens_result: int | None = None

    def add(self, *steps: StepSource) -> None:
        self.steps.extend(steps)

    async def list_models(self) -> Sequence[ModelInfo]:
        return self.models

    async def count_tokens(self, request: InferenceRequest) -> int | None:
        return self.count_tokens_result

    async def stream(self, request: InferenceRequest) -> AsyncIterator[CoreEvent]:
        self.requests.append(request)
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
        if step.text:
            blocks.append(Text(step.text))
        blocks.extend(step.calls)
        stop = step.stop or (StopReason.TOOL_USE if step.calls else StopReason.END_TURN)
        message = Message(uuid.uuid4().hex, Role.ASSISTANT, tuple(blocks), 0.0)
        yield Completed(message, step.usage or Usage(100, 10), stop)


def factory(options: dict[str, Any], paths: Any) -> ScriptedCore:
    return ScriptedCore(options.get("steps", ()), default=options.get("default"))
