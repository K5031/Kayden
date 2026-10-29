"""Reusable Core conformance suite.

Subclass `CoreSuite` and provide a `driver` fixture returning a `CoreDriver`. A scripted
driver queues provider responses on a stub; a live driver talks to the real API and
reports `scripted = False`, which skips the checks that need a scripted provider.
"""

from __future__ import annotations

import abc
import asyncio
import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

import pytest

from kayden.api import (
    Completed,
    Core,
    CoreError,
    CoreEvent,
    InferenceRequest,
    Message,
    Reasoning,
    ReasoningDelta,
    Role,
    StopReason,
    Text,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolResult,
    ToolSpec,
    ToolStatus,
    TransientCoreError,
    Usage,
    UsageReport,
)

WEATHER_TOOL = ToolSpec(
    name="get_weather",
    description="Get the current weather for a city.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
)


def user(text: str) -> Message:
    return Message(uuid.uuid4().hex, Role.USER, (Text(text),), time.time())


@dataclass(frozen=True)
class Expect:
    """What a scripted driver promises the provider returns; None means unchecked."""

    text: str | None = None
    tool_call: ToolCall | None = None
    reasoning: Reasoning | None = None
    usage: Usage | None = None
    stop: StopReason | None = None


@dataclass(frozen=True)
class Sent:
    """Provider-neutral view of the last request the stub received."""

    reasoning_states: list[dict] = field(default_factory=list)
    tool_call_ids: list[str] = field(default_factory=list)
    tool_results: list[tuple[str, str, bool]] = field(default_factory=list)  # id, text, error


class CoreDriver(abc.ABC):
    core: Core
    model: str
    scripted: bool = True
    resends_reasoning: bool = True  # False: provider format has no slot for reasoning input

    def request(self, messages: Sequence[Message], **kwargs) -> InferenceRequest:
        kwargs.setdefault("tools", [WEATHER_TOOL])
        kwargs.setdefault("max_output_tokens", 256)
        return InferenceRequest(self.model, "You are a terse assistant.", messages, **kwargs)

    @abc.abstractmethod
    def text_turn(self) -> tuple[InferenceRequest, Expect]: ...

    @abc.abstractmethod
    def tool_turn(self) -> tuple[InferenceRequest, Expect]:
        """A request whose answer calls WEATHER_TOOL."""

    def reasoning_turn(self) -> tuple[InferenceRequest, Expect] | None:
        return None

    def queue_text(self) -> Expect:
        """Prepare a plain text answer to the next request (live: no-op)."""
        return Expect()

    def queue_error(self, status: int, retry_after: float | None = None) -> None:
        raise NotImplementedError

    def queue_midstream_error(self) -> None:
        """A provider error after the stream has started (overload and the like)."""
        raise NotImplementedError

    def queue_hang(self) -> Callable[[], bool]:
        """Stream one text chunk, then stall. Returns a probe: was the response closed?"""
        raise NotImplementedError

    def requests_sent(self) -> int:
        raise NotImplementedError

    def last_sent(self) -> Sent | None:
        return None


async def collect(core: Core, request: InferenceRequest) -> list[CoreEvent]:
    return [event async for event in core.stream(request)]


def check_stream(events: Sequence[CoreEvent]) -> Completed:
    """Invariants every stream must satisfy; returns the Completed event."""
    completed = [e for e in events if isinstance(e, Completed)]
    assert len(completed) == 1, "exactly one Completed"
    assert isinstance(events[-1], Completed), "Completed is last"
    done = completed[0]
    message = done.message
    assert message.role is Role.ASSISTANT

    streamed_text = "".join(e.text for e in events if isinstance(e, TextDelta))
    final_text = "".join(b.text for b in message.blocks if isinstance(b, Text))
    assert streamed_text == final_text

    streamed_reasoning = "".join(e.text for e in events if isinstance(e, ReasoningDelta))
    final_reasoning = "".join(b.text for b in message.blocks if isinstance(b, Reasoning))
    assert streamed_reasoning == final_reasoning

    calls = [b for b in message.blocks if isinstance(b, ToolCall)]
    assert len({c.id for c in calls}) == len(calls), "tool call ids are unique"
    deltas = [e for e in events if isinstance(e, ToolCallDelta)]
    if deltas:
        by_id: dict[str, list[ToolCallDelta]] = {}
        for d in deltas:
            by_id.setdefault(d.call_id, []).append(d)
        assert set(by_id) == {c.id for c in calls}
        for call in calls:
            fragments = by_id[call.id]
            assert fragments[0].name == call.name
            raw = "".join(d.arguments_chunk for d in fragments)
            assert (json.loads(raw) if raw else {}) == call.arguments

    if calls:
        assert done.stop in (StopReason.TOOL_USE, StopReason.MAX_TOKENS)
    elif done.stop is StopReason.TOOL_USE:
        pytest.fail("TOOL_USE without tool calls")

    reports = [e.usage for e in events if isinstance(e, UsageReport)]
    for earlier, later in pairwise(reports):
        assert later.output_tokens >= earlier.output_tokens
        assert later.input_tokens >= earlier.input_tokens
    if reports:
        assert done.usage.output_tokens >= reports[-1].output_tokens
        assert done.usage.input_tokens >= reports[-1].input_tokens
    return done


def check_expect(done: Completed, expect: Expect) -> None:
    blocks = done.message.blocks
    if expect.text is not None:
        assert "".join(b.text for b in blocks if isinstance(b, Text)) == expect.text
    if expect.tool_call is not None:
        assert expect.tool_call in blocks
    if expect.reasoning is not None:
        assert expect.reasoning in blocks
    if expect.usage is not None:
        assert done.usage == expect.usage
    if expect.stop is not None:
        assert done.stop is expect.stop


class CoreSuite:
    """The Core contract. Subclasses provide a `driver` fixture."""

    def require_scripted(self, driver: CoreDriver) -> None:
        if not driver.scripted:
            pytest.skip("needs a scripted provider")

    async def test_text_stream(self, driver: CoreDriver) -> None:
        request, expect = driver.text_turn()
        events = await collect(driver.core, request)
        done = check_stream(events)
        check_expect(done, expect)
        assert done.stop is StopReason.END_TURN
        assert any(isinstance(e, TextDelta) for e in events)
        assert any(isinstance(b, Text) and b.text for b in done.message.blocks)
        assert done.usage.input_tokens > 0 and done.usage.output_tokens > 0

    async def test_usage_reported_during_stream(self, driver: CoreDriver) -> None:
        request, _ = driver.text_turn()
        events = await collect(driver.core, request)
        check_stream(events)
        assert any(isinstance(e, UsageReport) for e in events[:-1])

    async def test_tool_call_round_trip(self, driver: CoreDriver) -> None:
        request, expect = driver.tool_turn()
        done = check_stream(await collect(driver.core, request))
        check_expect(done, expect)
        assert done.stop is StopReason.TOOL_USE
        calls = [b for b in done.message.blocks if isinstance(b, ToolCall)]
        assert calls and all(c.name == WEATHER_TOOL.name for c in calls)
        assert all(isinstance(c.arguments.get("city"), str) for c in calls)

        results = tuple(ToolResult(c.id, ToolStatus.OK, "Sunny, 21 C") for c in calls)
        follow_up = driver.request(
            [*request.messages, done.message, Message("r", Role.TOOL, results, time.time())]
        )
        expect = driver.queue_text()
        done2 = check_stream(await collect(driver.core, follow_up))
        check_expect(done2, expect)
        assert done2.stop is StopReason.END_TURN

        sent = driver.last_sent()
        if sent is not None:
            assert sent.tool_call_ids == [c.id for c in calls]
            assert sent.tool_results == [(c.id, "Sunny, 21 C", False) for c in calls]

    async def test_error_tool_results_are_marked(self, driver: CoreDriver) -> None:
        self.require_scripted(driver)
        call = ToolCall("call_1", WEATHER_TOOL.name, {"city": "Oslo"})
        messages = [
            user("Weather in Oslo?"),
            Message("a", Role.ASSISTANT, (call,), time.time()),
            Message(
                "r", Role.TOOL, (ToolResult("call_1", ToolStatus.ERROR, "boom"),), time.time()
            ),
        ]
        driver.queue_text()
        check_stream(await collect(driver.core, driver.request(messages)))
        sent = driver.last_sent()
        assert sent is not None
        [(call_id, content, is_error)] = sent.tool_results
        assert call_id == "call_1" and "boom" in content and is_error

    async def test_reasoning_round_trip(self, driver: CoreDriver) -> None:
        turn = driver.reasoning_turn()
        if turn is None:
            pytest.skip("driver has no reasoning scenario")
        request, expect = turn
        done = check_stream(await collect(driver.core, request))
        check_expect(done, expect)
        reasoning = [b for b in done.message.blocks if isinstance(b, Reasoning)]
        assert reasoning

        follow_up = driver.request(
            [*request.messages, done.message, user("Thanks. One word: and now?")],
            max_output_tokens=request.max_output_tokens,
            options=request.options,
        )
        driver.queue_text()
        check_stream(await collect(driver.core, follow_up))
        sent = driver.last_sent()
        if sent is not None:
            expected = [r.state for r in reasoning] if driver.resends_reasoning else []
            assert sent.reasoning_states == expected

    @pytest.mark.parametrize("retry_after", [None, 7.0])
    @pytest.mark.parametrize("status", [429, 500, 503, 529])
    async def test_transient_errors(
        self, driver: CoreDriver, status: int, retry_after: float | None
    ) -> None:
        self.require_scripted(driver)
        driver.queue_error(status, retry_after)
        request, _ = driver.text_turn()
        with pytest.raises(TransientCoreError) as info:
            await collect(driver.core, request)
        assert info.value.retry_after == retry_after
        assert driver.requests_sent() == 1, "Core must not retry"

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    async def test_permanent_errors(self, driver: CoreDriver, status: int) -> None:
        self.require_scripted(driver)
        driver.queue_error(status)
        request, _ = driver.text_turn()
        with pytest.raises(CoreError) as info:
            await collect(driver.core, request)
        assert not isinstance(info.value, TransientCoreError)
        assert driver.requests_sent() == 1

    async def test_midstream_error_is_transient(self, driver: CoreDriver) -> None:
        self.require_scripted(driver)
        driver.queue_midstream_error()
        request, _ = driver.text_turn()
        events: list[CoreEvent] = []
        with pytest.raises(TransientCoreError):
            async for event in driver.core.stream(request):
                events.append(event)
        assert not any(isinstance(e, Completed) for e in events)

    async def test_cancel_stops_promptly(self, driver: CoreDriver) -> None:
        if driver.scripted:
            closed = driver.queue_hang()
            request, _ = driver.text_turn()
        else:
            closed = None
            request = driver.request(
                [user("Count from 1 to 500, one number per line.")], max_output_tokens=2048
            )
        first = asyncio.Event()

        async def consume() -> None:
            async for event in driver.core.stream(request):
                if isinstance(event, (TextDelta, ReasoningDelta)):
                    first.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(first.wait(), 30)
        task.cancel()
        start = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert time.monotonic() - start < 1
        if closed is not None:
            assert closed(), "provider response must be closed on cancel"

    async def test_count_tokens(self, driver: CoreDriver) -> None:
        request, _ = driver.text_turn()
        small = await driver.core.count_tokens(request)
        big = await driver.core.count_tokens(
            driver.request([*request.messages, user("lorem ipsum dolor " * 200)])
        )
        assert 0 < small < big

    async def test_list_models(self, driver: CoreDriver) -> None:
        models = await driver.core.list_models()
        assert models
        assert driver.model in {m.id for m in models}
        assert all(m.context_window > 0 and m.name for m in models)
