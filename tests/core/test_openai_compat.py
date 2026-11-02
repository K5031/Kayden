from __future__ import annotations

import json
import os
import re
import time

import httpx2
import openai
import pytest
from openai import AsyncOpenAI

from kayden.api import (
    InferenceRequest,
    InstancePaths,
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


# ── DeepSeek profile ─────────────────────────────────────────────────────


class DeepSeekDriver(OpenAIDriver):
    resends_reasoning = True

    def __init__(self, **config) -> None:
        super().__init__(profile="deepseek", **config)

    def last_sent(self) -> Sent:
        sent = super().last_sent()
        # DeepSeek's reasoning has no opaque state; "" is the placeholder for "none".
        sent.reasoning_states[:] = [None for m in self.stub.bodies[-1]["messages"]
                                    if m.get("reasoning_content")]
        return sent

    def queue_context_overflow(self):
        # Wording seen live on 2026-10-30; there is no dedicated error code.
        self.stub.queue(Reply(status=400, json_body={"error": {
            "message": "This model's maximum context length is 1048576 tokens. However, you"
                       " requested 2471803 tokens (2471787 in the messages, 16 in the"
                       " completion). Please reduce the length of the messages or completion."
                       " (request_id: 982fead7)",
            "type": "invalid_request_error", "code": "invalid_request_error"}}))


class TestDeepSeekConformance(CoreSuite):
    @pytest.fixture
    async def driver(self):
        driver = DeepSeekDriver()
        yield driver
        await driver.core.aclose()


@pytest.fixture
async def deepseek():
    driver = DeepSeekDriver()
    yield driver
    await driver.core.aclose()


def assistant(*blocks) -> Message:
    return Message("a", Role.ASSISTANT, tuple(blocks), time.time())


async def sent_assistants(driver: OpenAIDriver, messages: list[Message], **kwargs) -> list[dict]:
    driver.queue_text()
    await collect(driver.core, driver.request(messages, **kwargs))
    return [m for m in driver.stub.bodies[-1]["messages"] if m["role"] == "assistant"]


async def test_deepseek_replays_reasoning_of_the_same_model(deepseek: DeepSeekDriver) -> None:
    call = ToolCall("c", "get_weather", {"city": "Rome"})
    [sent] = await sent_assistants(deepseek, [
        user("q"), assistant(Reasoning("one ", model=MODEL), Reasoning("two", model=MODEL), call),
        Message("t", Role.TOOL, (ToolResult("c", ToolStatus.OK, "sun"),), time.time()),
    ])
    assert sent["reasoning_content"] == "one two" and sent["tool_calls"]


@pytest.mark.parametrize("blocks", [
    (Text("partial, committed by Stop or shrunk by Memory"),),
    (Reasoning("from another model", model="other"), Text("ok")),
    (Reasoning("from an unknown model"), Text("ok")),
], ids=["none", "other-model", "unknown-model"])
async def test_deepseek_sends_a_placeholder_without_replayable_reasoning(
    deepseek: DeepSeekDriver, blocks: tuple
) -> None:
    [sent] = await sent_assistants(deepseek, [user("q"), assistant(*blocks), user("again")])
    assert sent["reasoning_content"] == ""


async def test_deepseek_replays_without_tools(deepseek: DeepSeekDriver) -> None:
    # Live: omitting it is a 400 here too (thinking on, unknown call id), despite the docs.
    [sent] = await sent_assistants(
        deepseek, [user("q"), assistant(Reasoning("r", model=MODEL), Text("ok")), user("again")],
        tools=[])
    assert sent["reasoning_content"] == "r"


async def test_deepseek_drops_reasoning_only_turns(deepseek: DeepSeekDriver) -> None:
    messages = [user("q"), assistant(Reasoning("ran out of tokens", model=MODEL)), user("again")]
    assert await sent_assistants(deepseek, messages) == []


async def test_stock_profile_never_replays_reasoning(oai: OpenAIDriver) -> None:
    [sent] = await sent_assistants(
        oai, [user("q"), assistant(Reasoning("r", model=MODEL), Text("ok")), user("again")])
    assert "reasoning_content" not in sent


async def test_maintenance_options_apply_only_to_maintenance(deepseek: DeepSeekDriver) -> None:
    await sent_assistants(deepseek, [user("summarize")], maintenance=True)
    assert deepseek.stub.bodies[-1]["thinking"] == {"type": "disabled"}
    await sent_assistants(deepseek, [user("work")], tools=[])
    assert "thinking" not in deepseek.stub.bodies[-1]
    await sent_assistants(deepseek, [user("work")])
    assert "thinking" not in deepseek.stub.bodies[-1]


async def test_configured_options_win_over_maintenance_options() -> None:
    driver = DeepSeekDriver(options={"thinking": {"type": "enabled"}})
    await sent_assistants(driver, [user("summarize")], maintenance=True)
    assert driver.stub.bodies[-1]["thinking"] == {"type": "enabled"}
    await driver.core.aclose()


@pytest.mark.parametrize("finish", ["insufficient_system_resource", "aborted"])
async def test_deepseek_interrupted_generation_is_transient(
    deepseek: DeepSeekDriver, finish: str
) -> None:
    deepseek.stub.queue(Reply(events=text_events(["partial"], finish=finish)))
    events = []
    with pytest.raises(TransientCoreError, match=finish):
        async for event in deepseek.core.stream(deepseek.request([user("hi")])):
            events.append(event)
    assert any(isinstance(e, UsageReport) for e in events)  # the attempt is still charged


async def test_stock_profile_keeps_unknown_finish_reasons(oai: OpenAIDriver) -> None:
    oai.stub.queue(Reply(events=text_events(["hi"], finish="aborted")))
    done = check_stream(await collect(oai.core, oai.request([user("hi")])))
    assert done.stop is StopReason.END_TURN


async def test_deepseek_usage_on_the_last_content_chunk(deepseek: DeepSeekDriver) -> None:
    last = chunk(finish="stop")[1] | {"usage": {
        "prompt_tokens": 17, "completion_tokens": 7, "total_tokens": 24,
        "prompt_tokens_details": {"cached_tokens": 5},
        "prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 12}}
    deepseek.stub.queue(Reply(events=[chunk({"content": "Hi"}), (None, last), DONE]))
    done = check_stream(await collect(deepseek.core, deepseek.request([user("hi")])))
    assert done.usage == USAGE and done.stop is StopReason.END_TURN


async def test_deepseek_keep_alive_comments_are_ignored(deepseek: DeepSeekDriver) -> None:
    events = text_events(["Hi"])
    body = b": keep-alive\n\n" + b": keep-alive\n\n".join(
        b"data: " + (e[1].encode() if isinstance(e[1], str) else json.dumps(e[1]).encode())
        + b"\n\n" for e in events)
    deepseek.stub.queue(Reply(raw=body))
    done = check_stream(await collect(deepseek.core, deepseek.request([user("hi")])))
    assert done.message.blocks == (Text("Hi"),)


def deepseek_models(stub: StubProvider) -> None:
    stub.routes["/v1/models"] = lambda r: httpx2.Response(200, json={"object": "list", "data": [
        {"id": "deepseek-flash", "object": "model", "owned_by": "deepseek",
         "context_window": 1_048_576, "max_output_tokens": 393_216},
        {"id": "deepseek-new", "object": "model", "owned_by": "deepseek",
         "context_window": 1_048_576, "max_output_tokens": 393_216},
    ]})


async def test_deepseek_model_defaults() -> None:
    driver = DeepSeekDriver()
    deepseek_models(driver.stub)
    models = {m.id: m for m in await driver.core.list_models()}
    flash = models["deepseek-flash"]
    assert (flash.context_window, flash.max_output_tokens) == (1_048_576, 64_000)
    assert (flash.input_usd_per_mtok, flash.output_usd_per_mtok,
            flash.cached_input_usd_per_mtok) == (0.30, 1.20, 0.006)
    new = models["deepseek-new"]  # not in the profile: what the provider reports, no prices
    assert new.max_output_tokens == 393_216 and new.input_usd_per_mtok is None
    await driver.core.aclose()


async def test_configured_model_merges_with_profile_defaults() -> None:
    driver = DeepSeekDriver(models={"deepseek-flash": {"max_output_tokens": 8000,
                                                       "output_usd_per_mtok": 9.0}})
    deepseek_models(driver.stub)
    flash = {m.id: m for m in await driver.core.list_models()}["deepseek-flash"]
    assert flash.max_output_tokens == 8000 and flash.output_usd_per_mtok == 9.0
    assert flash.input_usd_per_mtok == 0.30 and flash.cached_input_usd_per_mtok == 0.006
    await driver.core.aclose()


async def test_factory_selects_a_profile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    core = factory({"profile": "deepseek"}, InstancePaths(tmp_path))
    assert isinstance(core, OpenAICompatCore)
    await core.aclose()
    with pytest.raises(ValueError, match="nonexistent"):
        factory({"profile": "nonexistent"}, InstancePaths(tmp_path))


async def test_deepseek_requires_its_own_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-openai-key")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        OpenAICompatCore(profile="deepseek")


OPENAI_SENTINELS = {
    "OPENAI_API_KEY": "sentinel-api-key",
    "OPENAI_BASE_URL": "http://sentinel-base.invalid/v1",
    "OPENAI_ORG_ID": "sentinel-org",
    "OPENAI_PROJECT_ID": "sentinel-project",
    "OPENAI_ADMIN_KEY": "sentinel-admin-key",
    "OPENAI_WEBHOOK_SECRET": "sentinel-webhook",
}


def capture_requests(monkeypatch, path: str) -> tuple[StubProvider, list[httpx2.Request]]:
    """The Core builds its real SDK client; only the transport is replaced."""
    stub = StubProvider(path)
    seen: list[httpx2.Request] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return await stub.handle(request)

    real = openai.AsyncOpenAI
    transport = httpx2.MockTransport(handle)
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: real(
        **kwargs, http_client=httpx2.AsyncClient(transport=transport)))
    return stub, seen


def request_text(request: httpx2.Request) -> str:
    return f"{request.url}\n{request.headers.raw}\n{request.content!r}"


async def test_deepseek_sends_nothing_from_the_openai_environment(monkeypatch) -> None:
    for name, value in OPENAI_SENTINELS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    stub, seen = capture_requests(monkeypatch, "/chat/completions")
    stub.routes["/models"] = lambda r: httpx2.Response(200, json={"object": "list", "data": []})
    stub.queue(Reply(events=text_events(["hi"])))

    core = OpenAICompatCore(profile="deepseek")
    await core.list_models()
    await collect(core, InferenceRequest("deepseek-flash", "s", [user("hi")], []))
    await core.aclose()

    assert len(seen) == 2
    for request in seen:
        assert request.url.host == "api.deepseek.com"
        assert request.headers["authorization"] == "Bearer deepseek-key"
        assert "openai-organization" not in request.headers
        assert "openai-project" not in request.headers
        text = request_text(request)
        assert not [v for v in OPENAI_SENTINELS.values() if v in text]


async def test_stock_profile_still_uses_the_openai_environment(monkeypatch) -> None:
    for name, value in OPENAI_SENTINELS.items():
        monkeypatch.setenv(name, value)
    stub, seen = capture_requests(monkeypatch, "/v1/chat/completions")
    stub.queue(Reply(events=text_events(["hi"])))

    core = OpenAICompatCore()
    await collect(core, InferenceRequest("m", "s", [user("hi")], []))
    await core.aclose()

    [request] = seen
    assert request.url.host == "sentinel-base.invalid"
    assert request.headers["authorization"] == "Bearer sentinel-api-key"
    assert request.headers["openai-organization"] == "sentinel-org"
    assert request.headers["openai-project"] == "sentinel-project"


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


LIVE_DEEPSEEK = os.environ.get("KAYDEN_LIVE_TESTS") == "1" and bool(
    os.environ.get("DEEPSEEK_API_KEY"))


class DeepSeekLiveDriver(CoreDriver):
    scripted = False
    resends_reasoning = True

    def __init__(self) -> None:
        self.core = OpenAICompatCore(profile="deepseek")
        self.model = os.environ.get("KAYDEN_DEEPSEEK_MODEL", "deepseek-flash")

    def request(self, messages, **kwargs) -> InferenceRequest:
        kwargs.setdefault("max_output_tokens", 2000)  # thinking counts against the limit
        return super().request(messages, **kwargs)

    def text_turn(self) -> tuple[InferenceRequest, Expect]:
        return self.request([user("Reply with exactly: hello")]), Expect()

    def tool_turn(self) -> tuple[InferenceRequest, Expect]:
        prompt = f"Use the {WEATHER_TOOL.name} tool to get the weather in Paris."
        return self.request([user(prompt)]), Expect()

    def reasoning_turn(self) -> tuple[InferenceRequest, Expect]:
        return self.request([user("What is 17 * 23? Answer with the number.")]), Expect()


@pytest.mark.skipif(not LIVE_DEEPSEEK, reason="set KAYDEN_LIVE_TESTS=1 and DEEPSEEK_API_KEY")
class TestDeepSeekLive(CoreSuite):
    @pytest.fixture
    async def driver(self):
        driver = DeepSeekLiveDriver()
        yield driver
        await driver.core.aclose()
