from __future__ import annotations

import json
import os
import time

import httpx2
import pytest
from anthropic import AsyncAnthropic

from kayden.api import (
    Message,
    Reasoning,
    Role,
    StopReason,
    Text,
    ToolCall,
    ToolResult,
    ToolStatus,
    Usage,
)
from kayden.core.anthropic import AnthropicCore
from tests.conformance.core_suite import (
    WEATHER_TOOL,
    CoreDriver,
    CoreSuite,
    Expect,
    Sent,
    collect,
    user,
)
from tests.core.stub import Reply, StubProvider

MODEL = "claude-test-1"
USAGE = Usage(input_tokens=17, output_tokens=7, cached_tokens=5)


def message_start() -> tuple[str, dict]:
    usage = {
        "input_tokens": 12,
        "output_tokens": 1,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 0,
    }
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": usage,
    }
    return "message_start", {"type": "message_start", "message": message}


def block(index: int, content_block: dict, deltas: list[dict]) -> list[tuple[str, dict]]:
    return [
        ("content_block_start", {"type": "content_block_start", "index": index,
                                 "content_block": content_block}),
        *[("content_block_delta", {"type": "content_block_delta", "index": index, "delta": d})
          for d in deltas],
        ("content_block_stop", {"type": "content_block_stop", "index": index}),
    ]


def finish(stop_reason: str) -> list[tuple[str, dict]]:
    return [
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 7}}),
        ("message_stop", {"type": "message_stop"}),
    ]


def text_block(index: int, chunks: list[str]) -> list[tuple[str, dict]]:
    return block(index, {"type": "text", "text": ""},
                 [{"type": "text_delta", "text": c} for c in chunks])


def error_body(kind: str) -> dict:
    return {"type": "error", "error": {"type": kind, "message": "stub error"}}


ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error",
               403: "permission_error", 404: "not_found_error", 429: "rate_limit_error",
               500: "api_error", 503: "api_error", 529: "overloaded_error"}


class AnthropicDriver(CoreDriver):
    def __init__(self) -> None:
        self.stub = StubProvider("/v1/messages")
        self.stub.routes["/v1/messages/count_tokens"] = lambda r: httpx2.Response(
            200, json={"input_tokens": len(r.content) // 4}
        )
        self.stub.routes["/v1/models"] = lambda r: httpx2.Response(200, json={
            "data": [{"type": "model", "id": MODEL, "display_name": "Claude Test",
                      "created_at": "2026-01-01T00:00:00Z", "lifecycle": "active",
                      "max_input_tokens": 200_000, "max_tokens": 64_000}],
            "has_more": False, "first_id": MODEL, "last_id": MODEL,
        })
        client = AsyncAnthropic(api_key="test", http_client=self.stub.client())
        self.core = AnthropicCore(client=client)
        self.model = MODEL

    def queue_text(self) -> Expect:
        self.stub.queue(Reply(events=[message_start(), *text_block(0, ["Hel", "lo", "!"]),
                                      *finish("end_turn")]))
        return Expect(text="Hello!", usage=USAGE, stop=StopReason.END_TURN)

    def text_turn(self):
        return self.request([user("Say hello")]), self.queue_text()

    def tool_turn(self):
        chunks = ['{"ci', 'ty": "Pa', 'ris"}']
        events = [message_start(), *text_block(0, ["Checking."]),
                  *block(1, {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                             "input": {}},
                         [{"type": "input_json_delta", "partial_json": c} for c in chunks]),
                  *finish("tool_use")]
        self.stub.queue(Reply(events=events))
        call = ToolCall("toolu_1", "get_weather", {"city": "Paris"})
        return self.request([user("Weather in Paris?")]), Expect(tool_call=call)

    def reasoning_turn(self):
        events = [message_start(),
                  *block(0, {"type": "thinking", "thinking": "", "signature": ""},
                         [{"type": "thinking_delta", "thinking": "Let me "},
                          {"type": "thinking_delta", "thinking": "think."},
                          {"type": "signature_delta", "signature": "sig-abc"}]),
                  *block(1, {"type": "redacted_thinking", "data": "opaque"}, []),
                  *text_block(2, ["Done."]),
                  *finish("end_turn")]
        self.stub.queue(Reply(events=events))
        expected = Reasoning("Let me think.", {"provider": "anthropic", "signature": "sig-abc"})
        return self.request([user("Think first")]), Expect(reasoning=expected)

    def queue_error(self, status, retry_after=None):
        headers = {} if retry_after is None else {"retry-after": str(retry_after)}
        self.stub.queue(Reply(status=status, json_body=error_body(ERROR_TYPES[status]),
                              headers=headers))

    def queue_midstream_error(self):
        self.stub.queue(Reply(events=[message_start(), *text_block(0, ["Hi"])[:2],
                                      ("error", error_body("overloaded_error"))]))

    def queue_hang(self):
        reply = self.stub.queue(Reply(events=[message_start(), *text_block(0, ["Hi"])[:2]],
                                      hang=True))
        return lambda: reply.stream is not None and reply.stream.closed

    def requests_sent(self) -> int:
        return len(self.stub.bodies)

    def last_sent(self) -> Sent:
        sent = Sent()
        for message in self.stub.bodies[-1]["messages"]:
            content = message["content"]
            for b in content if isinstance(content, list) else []:
                match b["type"]:
                    case "thinking":
                        sent.reasoning_states.append(
                            {"provider": "anthropic", "signature": b["signature"]})
                    case "redacted_thinking":
                        sent.reasoning_states.append(
                            {"provider": "anthropic", "redacted": b["data"]})
                    case "tool_use":
                        sent.tool_call_ids.append(b["id"])
                    case "tool_result":
                        sent.tool_results.append(
                            (b["tool_use_id"], b["content"], b.get("is_error", False)))
        return sent


class TestAnthropicConformance(CoreSuite):
    @pytest.fixture
    async def driver(self):
        driver = AnthropicDriver()
        yield driver
        await driver.core.aclose()


# ── Adapter-specific translation ─────────────────────────────────────────


@pytest.fixture
async def anthropic():
    driver = AnthropicDriver()
    yield driver
    await driver.core.aclose()


async def test_request_translation(anthropic: AnthropicDriver) -> None:
    call = ToolCall("toolu_9", "get_weather", {"city": "Rome"})
    messages = [
        user("first"),
        Message("a", Role.ASSISTANT, (Reasoning("foreign", {"provider": "other"}),
                                      Text(""), Text("Looking."), call), time.time()),
        Message("t", Role.TOOL, (ToolResult("toolu_9", ToolStatus.INTERRUPTED, "gone"),),
                time.time()),
        user("again"),
    ]
    anthropic.queue_text()
    request = anthropic.request(messages, max_output_tokens=None,
                                options={"temperature": 0.2, "max_tokens": 99})
    await collect(anthropic.core, request)
    body = anthropic.stub.bodies[-1]
    assert body["system"] == "You are a terse assistant."
    assert body["temperature"] == 0.2 and body["max_tokens"] == 99
    assert body["stream"] is True
    assert body["tools"] == [{"name": "get_weather", "description": WEATHER_TOOL.description,
                              "input_schema": WEATHER_TOOL.parameters}]
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "first"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "toolu_9", "name": "get_weather",
             "input": {"city": "Rome"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_9", "content": "[interrupted] gone",
             "is_error": True},
            {"type": "text", "text": "again"},
        ]},
    ]


async def test_default_max_tokens_and_empty_system(anthropic: AnthropicDriver) -> None:
    anthropic.queue_text()
    request = anthropic.request([user("hi")], max_output_tokens=None, tools=[])
    request = type(request)(request.model, "", request.messages, request.tools)
    await collect(anthropic.core, request)
    body = anthropic.stub.bodies[-1]
    assert "system" not in body and "tools" not in body
    assert body["max_tokens"] == 8192


async def test_truncated_stream_is_transient(anthropic: AnthropicDriver) -> None:
    from kayden.api import TransientCoreError

    anthropic.stub.queue(Reply(events=[message_start(), *text_block(0, ["Hi"])]))
    with pytest.raises(TransientCoreError):
        await collect(anthropic.core, anthropic.request([user("hi")]))


async def test_context_overflow_is_permanent(anthropic: AnthropicDriver) -> None:
    from kayden.api import CoreError, TransientCoreError

    anthropic.stub.queue(Reply(status=400, json_body={"type": "error", "error": {
        "type": "invalid_request_error", "message": "prompt is too long: 300000 tokens"}}))
    with pytest.raises(CoreError) as info:
        await collect(anthropic.core, anthropic.request([user("hi")]))
    assert not isinstance(info.value, TransientCoreError)
    assert "prompt is too long" in str(info.value)


async def test_tool_call_without_arguments(anthropic: AnthropicDriver) -> None:
    anthropic.stub.queue(Reply(events=[
        message_start(),
        *block(0, {"type": "tool_use", "id": "toolu_2", "name": "get_weather", "input": {}}, []),
        *finish("tool_use"),
    ]))
    events = await collect(anthropic.core, anthropic.request([user("hi")]))
    assert events[-1].message.blocks == (ToolCall("toolu_2", "get_weather", {}),)


# ── Live (opt-in) ────────────────────────────────────────────────────────

LIVE = os.environ.get("KAYDEN_LIVE_TESTS") == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))


class AnthropicLiveDriver(CoreDriver):
    scripted = False

    def __init__(self) -> None:
        self.core = AnthropicCore()
        self.model = os.environ.get("KAYDEN_ANTHROPIC_MODEL", "claude-haiku-5-5")

    def text_turn(self):
        return self.request([user("Reply with exactly: hello")]), Expect()

    def tool_turn(self):
        options = {"tool_choice": {"type": "tool", "name": WEATHER_TOOL.name}}
        return self.request([user("What's the weather in Paris?")], options=options), Expect()

    def reasoning_turn(self):
        thinking = json.loads(os.environ.get(
            "KAYDEN_ANTHROPIC_THINKING", '{"type": "enabled", "budget_tokens": 1024}'))
        request = self.request([user("What is 17 * 23? Answer with the number.")],
                               max_output_tokens=4096, options={"thinking": thinking})
        return request, Expect()


@pytest.mark.skipif(not LIVE, reason="set KAYDEN_LIVE_TESTS=1 and ANTHROPIC_API_KEY")
class TestAnthropicLive(CoreSuite):
    @pytest.fixture
    async def driver(self):
        driver = AnthropicLiveDriver()
        yield driver
        await driver.core.aclose()


async def test_foreign_tool_ids_are_sanitized(anthropic: AnthropicDriver) -> None:
    call = ToolCall("functions.get_weather:0", "get_weather", {"city": "Rome"})
    messages = [user("hi"), Message("a", Role.ASSISTANT, (call,), time.time()),
                Message("t", Role.TOOL, (ToolResult(call.id, ToolStatus.OK, "ok"),), time.time())]
    anthropic.queue_text()
    await collect(anthropic.core, anthropic.request(messages))
    sent = anthropic.last_sent()
    assert sent.tool_call_ids == ["functions_get_weather_0"]
    assert sent.tool_results == [("functions_get_weather_0", "ok", False)]
