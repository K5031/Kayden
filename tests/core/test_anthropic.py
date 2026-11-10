from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from typing import Any

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
from kayden.core._common import ModelConfig
from kayden.core.anthropic import AnthropicCore, Prefix, factory, to_messages
from tests.conformance.core_suite import (
    WEATHER_TOOL,
    CoreDriver,
    CoreSuite,
    Expect,
    Sent,
    check_expect,
    check_stream,
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
    def __init__(self, **config) -> None:
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
        self.core = AnthropicCore(client=client, **config)
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
        request = self.request([user("Think first")])
        _, bound = to_messages(request.messages, MODEL, request.system, request.tools)
        expected = Reasoning("Let me think.", {"provider": "anthropic", "signature": "sig-abc",
                                               "prefix": bound}, MODEL)
        return request, Expect(reasoning=expected)

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

    def queue_raw_tool_call(self, raw_arguments):
        self.stub.queue(Reply(events=[
            message_start(),
            *block(0, {"type": "tool_use", "id": "toolu_r", "name": "get_weather", "input": {}},
                   [{"type": "input_json_delta", "partial_json": raw_arguments}]),
            *finish("tool_use"),
        ]))

    def queue_context_overflow(self):
        self.stub.queue(Reply(status=400, json_body={"type": "error", "error": {
            "type": "invalid_request_error",
            "message": "prompt is too long: 300000 tokens > 200000 maximum"}}))

    def queue_refusal(self):
        self.stub.queue(Reply(events=[message_start(), *text_block(0, ["I can't help."]),
                                      *finish("refusal")]))

    def requests_sent(self) -> int:
        return len(self.stub.bodies)

    def last_sent(self) -> Sent:
        sent = Sent()
        body = self.stub.bodies[-1]
        for i, message in enumerate(body["messages"]):
            content = message["content"]
            # What reasoning in this turn is bound to: everything sent before it.
            bound = Prefix(body.get("system", ""), body.get("tools", [])).of(body["messages"][:i])
            for b in content if isinstance(content, list) else []:
                match b["type"]:
                    case "text" if message["role"] == "user":
                        sent.user_texts.append(b["text"])
                    case "thinking":
                        sent.reasoning_states.append(
                            {"provider": "anthropic", "signature": b["signature"],
                             "prefix": bound})
                    case "redacted_thinking":
                        sent.reasoning_states.append(
                            {"provider": "anthropic", "redacted": b["data"], "prefix": bound})
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


async def test_request_translation() -> None:
    anthropic = AnthropicDriver(options={"temperature": 0.2, "max_tokens": 50},
                                models={MODEL: ModelConfig(options={"max_tokens": 99})})
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
    request = anthropic.request(messages, max_output_tokens=None)
    await collect(anthropic.core, request)
    await anthropic.core.aclose()
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


def thinking_sent(driver: AnthropicDriver) -> list[str]:
    return [b["signature"] for m in driver.stub.bodies[-1]["messages"] for b in m["content"]
            if b["type"] == "thinking"]


async def test_reasoning_is_replayed_only_while_everything_before_it_is_unchanged(
        anthropic: AnthropicDriver) -> None:
    """The API rejects a thinking block once the system prompt, the tools or an earlier
    message differ from when it was produced."""
    request, _ = anthropic.reasoning_turn()
    answer = check_stream(await collect(anthropic.core, request)).message
    history = [*request.messages, answer, user("And now?")]

    async def sent(**changes: Any) -> list[str]:
        anthropic.queue_text()
        follow_up = replace(anthropic.request(history), **changes)
        check_stream(await collect(anthropic.core, follow_up))
        return thinking_sent(anthropic)

    assert await sent() == ["sig-abc"]
    assert await sent(system="You are a terse assistant.\n\nYou work autonomously.") == []
    assert await sent(tools=[WEATHER_TOOL, replace(WEATHER_TOOL, name="idle")]) == []
    assert await sent(messages=[user("Think again"), answer, user("And now?")]) == []
    assert await sent(messages=[*history, answer, user("Once more")]) == ["sig-abc"]
    unbound = Message("a", Role.ASSISTANT, (
        Reasoning("old", {"provider": "anthropic", "signature": "sig-old"}, MODEL), Text("Done.")),
        time.time())
    assert await sent(messages=[*request.messages, unbound, user("And now?")]) == []


async def test_rejected_reasoning_is_left_out_on_a_second_try(anthropic: AnthropicDriver) -> None:
    request, _ = anthropic.reasoning_turn()
    answer = check_stream(await collect(anthropic.core, request)).message
    anthropic.stub.queue(Reply(status=400, json_body={"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "messages.1.content.0: Invalid `signature` in `thinking` block. The block "
                   "is bound to a different conversation. Remove the block."}}))
    expect = anthropic.queue_text()
    done = check_stream(await collect(
        anthropic.core, anthropic.request([*request.messages, answer, user("And now?")])))
    check_expect(done, expect)
    assert thinking_sent(anthropic) == []
    assert [b["type"] for b in anthropic.stub.bodies[-1]["messages"][1]["content"]] == ["text"]


async def test_truncated_stream_is_transient(anthropic: AnthropicDriver) -> None:
    from kayden.api import TransientCoreError

    anthropic.stub.queue(Reply(events=[message_start(), *text_block(0, ["Hi"])]))
    with pytest.raises(TransientCoreError):
        await collect(anthropic.core, anthropic.request([user("hi")]))


async def test_other_invalid_requests_are_permanent(anthropic: AnthropicDriver) -> None:
    from kayden.api import ContextOverflowError, CoreError, TransientCoreError

    anthropic.stub.queue(Reply(status=400, json_body={"type": "error", "error": {
        "type": "invalid_request_error", "message": "tools.0: bad schema"}}))
    with pytest.raises(CoreError) as info:
        await collect(anthropic.core, anthropic.request([user("hi")]))
    assert not isinstance(info.value, (TransientCoreError, ContextOverflowError))


async def test_model_configuration() -> None:
    anthropic = AnthropicDriver(models={
        MODEL: ModelConfig(context_window=100_000, input_usd_per_mtok=3.0,
                           output_usd_per_mtok=15.0, cached_input_usd_per_mtok=0.3)})
    [info] = await anthropic.core.list_models()
    assert (info.context_window, info.max_output_tokens) == (100_000, 64_000)
    assert (info.input_usd_per_mtok, info.output_usd_per_mtok,
            info.cached_input_usd_per_mtok) == (3.0, 15.0, 0.3)
    await anthropic.core.aclose()

    plain = AnthropicDriver()
    [info] = await plain.core.list_models()
    assert info.context_window == 200_000 and info.input_usd_per_mtok is None
    await plain.core.aclose()


async def test_built_in_prices_merge_with_configured_ones() -> None:
    driver = AnthropicDriver(models={"claude-opus-5-5": {"input_usd_per_mtok": 3.5}})
    driver.stub.routes["/v1/models"] = lambda r: httpx2.Response(200, json={
        "data": [{"type": "model", "id": m, "display_name": m, "lifecycle": "active",
                  "created_at": "2026-01-01T00:00:00Z", "max_input_tokens": 1_000_000,
                  "max_tokens": 128_000} for m in ("claude-opus-5-5", "claude-sonnet-5-5")],
        "has_more": False, "first_id": "claude-opus-5-5", "last_id": "claude-sonnet-5-5"})
    opus, sonnet = await driver.core.list_models()
    assert (opus.input_usd_per_mtok, opus.output_usd_per_mtok,
            opus.cached_input_usd_per_mtok) == (3.5, 20.0, 0.2)
    assert (sonnet.input_usd_per_mtok, sonnet.output_usd_per_mtok) == (2.0, 10.0)
    await driver.core.aclose()


async def test_factory(tmp_path) -> None:
    from kayden.api import InstancePaths

    core = factory({"api_key": "k", "options": {"temperature": 0},
                    "models": {MODEL: {"input_usd_per_mtok": 1.0}}}, InstancePaths(tmp_path))
    assert isinstance(core, AnthropicCore)
    await core.aclose()


async def test_missing_key_fails_at_construction(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        AnthropicCore()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    await AnthropicCore().aclose()


async def test_key_from_the_keychain(monkeypatch, keychain) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    keychain.store[("kayden", "ANTHROPIC_API_KEY")] = "from-keychain"
    core = AnthropicCore()
    assert core._client.api_key == "from-keychain"
    await core.aclose()


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
    """Thinking is adapter configuration, so it is on for every live request."""

    scripted = False

    def __init__(self) -> None:
        thinking = json.loads(os.environ.get(
            "KAYDEN_ANTHROPIC_THINKING", '{"type": "enabled", "budget_tokens": 1024}'))
        self.core = AnthropicCore(options={"thinking": thinking})
        self.model = os.environ.get("KAYDEN_ANTHROPIC_MODEL", "claude-haiku-5-5")

    def request(self, messages, **kwargs):
        kwargs.setdefault("max_output_tokens", 4096)
        return super().request(messages, **kwargs)

    def text_turn(self):
        return self.request([user("Reply with exactly: hello")]), Expect()

    def tool_turn(self):
        prompt = "Use the get_weather tool to get the weather in Paris."
        return self.request([user(prompt)]), Expect()

    def reasoning_turn(self):
        return self.request([user("What is 17 * 23? Answer with the number.")]), Expect()


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
