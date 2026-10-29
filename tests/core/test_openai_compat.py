from __future__ import annotations

import os
import re
import time

import httpx2
import pytest
from openai import AsyncOpenAI

from kayden.api import (
    InferenceRequest,
    Message,
    Reasoning,
    Role,
    StopReason,
    Text,
    ToolCall,
    ToolResult,
    ToolStatus,
    TransientCoreError,
    Usage,
    UsageReport,
)
from kayden.core._common import ModelConfig
from kayden.core.openai_compat import OpenAICompatCore, factory
from tests.conformance.core_suite import (
    WEATHER_TOOL,
    CoreDriver,
    CoreSuite,
    Expect,
    Sent,
    check_stream,
    collect,
    user,
)
from tests.core.stub import Reply, StubProvider

MODEL = "local-test"
USAGE = Usage(input_tokens=17, output_tokens=7, cached_tokens=5)
_STATUS_PREFIX = re.compile(r"^\[(error|cancelled|abandoned|timed_out|interrupted|refused)\]")


def chunk(delta: dict | None = None, finish: str | None = None) -> tuple[None, dict]:
    return None, {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": MODEL,
                  "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


def usage_chunk() -> tuple[None, dict]:
    return None, {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": MODEL,
                  "choices": [], "usage": {"prompt_tokens": 17, "completion_tokens": 7,
                                           "total_tokens": 24,
                                           "prompt_tokens_details": {"cached_tokens": 5}}}


DONE = (None, "[DONE]")


def text_events(chunks: list[str], finish: str = "stop", usage: bool = True) -> list:
    return [chunk({"role": "assistant", "content": ""}),
            *[chunk({"content": c}) for c in chunks],
            chunk(finish=finish), *([usage_chunk()] if usage else []), DONE]


class OpenAIDriver(CoreDriver):
    resends_reasoning = False

    def __init__(self, **config) -> None:
        self.stub = StubProvider("/v1/chat/completions")
        self.stub.routes["/v1/models"] = lambda r: httpx2.Response(200, json={
            "object": "list",
            "data": [{"id": MODEL, "object": "model", "created": 1, "owned_by": "me",
                      "max_model_len": 32_768},
                     {"id": "other", "object": "model", "created": 1, "owned_by": "me"}],
        })
        client = AsyncOpenAI(api_key="test", base_url="http://stub/v1",
                             http_client=self.stub.client())
        self.core = OpenAICompatCore(client=client, **config)
        self.model = MODEL

    def queue_text(self) -> Expect:
        self.stub.queue(Reply(events=text_events(["Hel", "lo", "!"])))
        return Expect(text="Hello!", usage=USAGE, stop=StopReason.END_TURN)

    def text_turn(self):
        return self.request([user("Say hello")]), self.queue_text()

    def tool_turn(self):
        events = [
            chunk({"role": "assistant", "content": "Checking."}),
            chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                   "function": {"name": "get_weather", "arguments": ""}}]}),
            *[chunk({"tool_calls": [{"index": 0, "function": {"arguments": a}}]})
              for a in ['{"ci', 'ty": "Pa', 'ris"}']],
            chunk({"tool_calls": [{"index": 1, "id": "call_2", "type": "function",
                                   "function": {"name": "get_weather",
                                                "arguments": '{"city": "Oslo"}'}}]}),
            chunk(finish="tool_calls"), usage_chunk(), DONE,
        ]
        self.stub.queue(Reply(events=events))
        call = ToolCall("call_1", "get_weather", {"city": "Paris"})
        return self.request([user("Weather in Paris and Oslo?")]), Expect(tool_call=call)

    def reasoning_turn(self):
        events = [chunk({"role": "assistant", "reasoning_content": "Let me "}),
                  chunk({"reasoning_content": "think."}),
                  *text_events(["Done."])]
        self.stub.queue(Reply(events=events))
        expected = Reasoning("Let me think.", None, MODEL)
        return self.request([user("Think first")]), Expect(reasoning=expected)

    def queue_error(self, status, retry_after=None):
        headers = {} if retry_after is None else {"retry-after": str(retry_after)}
        body = {"error": {"message": "stub error", "type": "server_error", "code": None}}
        self.stub.queue(Reply(status=status, json_body=body, headers=headers))

    def queue_midstream_error(self):
        self.stub.queue(Reply(events=[chunk({"content": "Hi"}),
                                      (None, {"error": {"message": "overloaded"}})]))

    def queue_hang(self):
        reply = self.stub.queue(Reply(events=[chunk({"content": "Hi"})], hang=True))
        return lambda: reply.stream is not None and reply.stream.closed

    def queue_raw_tool_call(self, raw_arguments):
        self.stub.queue(Reply(events=[
            chunk({"tool_calls": [{"index": 0, "id": "call_r", "type": "function",
                                   "function": {"name": "get_weather",
                                                "arguments": raw_arguments}}]}),
            chunk(finish="tool_calls"), usage_chunk(), DONE,
        ]))

    def queue_context_overflow(self):
        self.stub.queue(Reply(status=400, json_body={"error": {
            "message": "This model's maximum context length is 32768 tokens. However, you"
                       " requested 40000 tokens.",
            "type": "invalid_request_error", "code": "context_length_exceeded"}}))

    def queue_refusal(self):
        self.stub.queue(Reply(events=text_events(["I can't"], finish="content_filter")))

    def requests_sent(self) -> int:
        return len(self.stub.bodies)

    def last_sent(self) -> Sent:
        sent = Sent()
        for message in self.stub.bodies[-1]["messages"]:
            if message["role"] == "user":
                sent.user_texts.append(message["content"])
            for call in message.get("tool_calls", []):
                sent.tool_call_ids.append(call["id"])
            if message["role"] == "tool":
                content = message["content"]
                sent.tool_results.append(
                    (message["tool_call_id"], content, bool(_STATUS_PREFIX.match(content))))
            if "reasoning_content" in message or "reasoning" in message:
                sent.reasoning_states.append({})
        return sent


class TestOpenAICompatConformance(CoreSuite):
    @pytest.fixture
    async def driver(self):
        driver = OpenAIDriver()
        yield driver
        await driver.core.aclose()


# ── Adapter-specific translation ─────────────────────────────────────────


@pytest.fixture
async def oai():
    driver = OpenAIDriver()
    yield driver
    await driver.core.aclose()


async def test_request_translation() -> None:
    oai = OpenAIDriver(options={"temperature": 0.2, "seed": 1},
                       models={MODEL: ModelConfig(options={"seed": 7})})
    call = ToolCall("call_9", "get_weather", {"city": "Rome"})
    messages = [
        user("first"),
        Message("a", Role.ASSISTANT, (Reasoning("hidden", {"provider": "anthropic"}),
                                      Text("Looking."), call), time.time()),
        Message("t", Role.TOOL, (ToolResult("call_9", ToolStatus.TIMED_OUT, "slow"),),
                time.time()),
        user("again"),
    ]
    oai.queue_text()
    await collect(oai.core, oai.request(messages))
    await oai.core.aclose()
    body = oai.stub.bodies[-1]
    assert body["temperature"] == 0.2 and body["seed"] == 7 and body["max_tokens"] == 256
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["tools"] == [{"type": "function", "function": {
        "name": "get_weather", "description": WEATHER_TOOL.description,
        "parameters": WEATHER_TOOL.parameters}}]
    assert body["messages"] == [
        {"role": "system", "content": "You are a terse assistant."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "Looking.", "tool_calls": [
            {"id": "call_9", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city": "Rome"}'}}]},
        {"role": "tool", "tool_call_id": "call_9", "content": "[timed_out] slow"},
        {"role": "user", "content": "again"},
    ]


async def test_list_models_context_windows(oai: OpenAIDriver) -> None:
    models = {m.id: m for m in await oai.core.list_models()}
    assert models[MODEL].context_window == 32_768
    assert models["other"].context_window is None  # unknown: never a silent default

    configured = OpenAIDriver(models={"other": ModelConfig(
        context_window=4096, max_output_tokens=512, input_usd_per_mtok=0.5,
        output_usd_per_mtok=1.5)})
    other = {m.id: m for m in await configured.core.list_models()}["other"]
    assert (other.context_window, other.max_output_tokens) == (4096, 512)
    assert (other.input_usd_per_mtok, other.output_usd_per_mtok) == (0.5, 1.5)
    await configured.core.aclose()


async def test_factory(tmp_path) -> None:
    from kayden.api import InstancePaths

    core = factory({"base_url": "http://localhost:1/v1",
                    "models": {"m": {"context_window": 8192}}}, InstancePaths(tmp_path))
    assert isinstance(core, OpenAICompatCore)
    await core.aclose()


async def test_missing_usage_is_estimated(oai: OpenAIDriver) -> None:
    oai.stub.queue(Reply(events=text_events(["Hello there"], usage=False)))
    events = await collect(oai.core, oai.request([user("hi")]))
    done = check_stream(events)
    assert not any(isinstance(e, UsageReport) for e in events)
    assert done.usage.input_tokens > 0 and done.usage.output_tokens > 0


async def test_tool_calls_with_stop_finish_reason(oai: OpenAIDriver) -> None:
    # Some local servers report "stop" even when the turn ends in tool calls.
    oai.stub.queue(Reply(events=[
        chunk({"tool_calls": [{"index": 0, "function": {"name": "get_weather",
                                                        "arguments": '{"city": "Rome"}'}}]}),
        chunk(finish="stop"), DONE,
    ]))
    done = check_stream(await collect(oai.core, oai.request([user("hi")])))
    assert done.stop is StopReason.TOOL_USE
    [call] = done.message.blocks
    assert isinstance(call, ToolCall) and call.id and call.arguments == {"city": "Rome"}


async def test_truncated_stream_is_transient(oai: OpenAIDriver) -> None:
    oai.stub.queue(Reply(events=[chunk({"content": "Hi"}), DONE]))
    with pytest.raises(TransientCoreError):
        await collect(oai.core, oai.request([user("hi")]))


# ── Live (opt-in) ────────────────────────────────────────────────────────

LIVE = os.environ.get("KAYDEN_LIVE_TESTS") == "1" and bool(os.environ.get("KAYDEN_OPENAI_MODEL"))


class OpenAILiveDriver(CoreDriver):
    scripted = False
    resends_reasoning = False

    def __init__(self) -> None:
        self.core = OpenAICompatCore(base_url=os.environ.get("KAYDEN_OPENAI_BASE_URL"))
        self.model = os.environ["KAYDEN_OPENAI_MODEL"]

    def text_turn(self) -> tuple[InferenceRequest, Expect]:
        return self.request([user("Reply with exactly: hello")]), Expect()

    def tool_turn(self) -> tuple[InferenceRequest, Expect]:
        prompt = f"Use the {WEATHER_TOOL.name} tool to get the weather in Paris."
        return self.request([user(prompt)]), Expect()


@pytest.mark.skipif(not LIVE, reason="set KAYDEN_LIVE_TESTS=1 and KAYDEN_OPENAI_MODEL")
class TestOpenAICompatLive(CoreSuite):
    @pytest.fixture
    async def driver(self):
        driver = OpenAILiveDriver()
        yield driver
        await driver.core.aclose()
