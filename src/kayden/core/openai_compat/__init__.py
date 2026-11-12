"""Core for OpenAI-compatible chat completions (OpenAI, DeepSeek, vLLM, llama.cpp, Ollama, ...)."""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx2
import openai

from kayden.api import (
    JSON,
    Block,
    Completed,
    ContextOverflowError,
    Core,
    CoreError,
    CoreEvent,
    Document,
    File,
    Image,
    InferenceRequest,
    InstancePaths,
    Message,
    ModelInfo,
    OperationResult,
    Reasoning,
    ReasoningDelta,
    Role,
    ServerToolUse,
    StopReason,
    Text,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolResult,
    ToolSpec,
    TransientCoreError,
    Usage,
    UsageReport,
    attachment_text,
)
from kayden.core._common import (
    ModelConfig,
    assistant_message,
    configured,
    merged_configs,
    model_configs,
    operation_text,
    parse_arguments,
    request_options,
    result_text,
    retry_after,
    server_tool_text,
    stored_key,
)

_CONTEXT_WINDOW_FIELDS = ("context_length", "max_model_len", "context_window")
_MAX_OUTPUT_FIELDS = ("max_output_tokens",)
_REASONING_FIELDS = ("reasoning_content", "reasoning")  # non-standard, server-dependent
_STOP_REASONS = {
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
}
_OVERFLOW_CODES = ("context_length_exceeded", "string_above_max_length")
_OVERFLOW_MESSAGES = ("maximum context length", "context length", "context window",
                      "too many tokens", "prompt is too long")


@dataclass(frozen=True, slots=True)
class Profile:
    """What a server does differently from stock OpenAI chat completions."""

    base_url: str | None = None  # set: nothing is taken from the OpenAI environment
    api_key_env: str = "OPENAI_API_KEY"
    api_key_required: bool = False  # a missing key fails at construction
    models: Mapping[str, ModelConfig] = field(default_factory=dict)  # defaults under `models`
    transient_finish_reasons: frozenset[str] = frozenset()  # interrupted, worth retrying
    reasoning_input_field: str | None = None  # assistant field that replays reasoning
    images: bool = True  # its models take images; False: an attached one goes as a line
    maintenance_options: Mapping[str, Any] = field(default_factory=dict)  # under `options`


# Prices are the peak rates of 2026-10-30: off-peak is half, and ModelInfo holds one price.
_PROFILES = {
    "openai": Profile(),
    "deepseek": Profile(
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        api_key_required=True,
        models={
            "deepseek-flash": ModelConfig(
                max_output_tokens=64_000, input_usd_per_mtok=0.30, output_usd_per_mtok=1.20,
                cached_input_usd_per_mtok=0.006),
            "deepseek-v4-pro": ModelConfig(
                max_output_tokens=64_000, input_usd_per_mtok=1.32, output_usd_per_mtok=3.96,
                cached_input_usd_per_mtok=0.044),
        },
        transient_finish_reasons=frozenset({"insufficient_system_resource", "aborted"}),
        reasoning_input_field="reasoning_content",
        images=False,
        maintenance_options={"thinking": {"type": "disabled"}},
    ),
}


def factory(options: dict[str, Any], paths: InstancePaths) -> OpenAICompatCore:
    """Options: profile (default "openai"), api_key (default: the profile's variable),
    base_url, options, models, max_tokens_param, stream_usage; see OpenAICompatCore."""
    return OpenAICompatCore(**options)


def deepseek_factory(options: dict[str, Any], paths: InstancePaths) -> OpenAICompatCore:
    """The DeepSeek profile; options as for factory."""
    return OpenAICompatCore(**{"profile": "deepseek", **options})


def openai_factory(options: dict[str, Any], paths: InstancePaths) -> OpenAICompatCore:
    """The OpenAI profile; options as for factory."""
    return OpenAICompatCore(**{"profile": "openai", **options})


deepseek_factory.display_name = "DeepSeek"  # type: ignore[attr-defined]
deepseek_factory.key_name = "DEEPSEEK_API_KEY"  # type: ignore[attr-defined]
openai_factory.display_name = "OpenAI"  # type: ignore[attr-defined]
openai_factory.key_name = "OPENAI_API_KEY"  # type: ignore[attr-defined]


class OpenAICompatCore(Core):
    """`options` are merged verbatim into every request body (temperature,
    reasoning_effort, tool_choice, ...); `models` adds per-model options, context windows
    and prices. `profile` names the server's differences (see Profile). Reasoning text from
    servers that stream it is surfaced, and sent back only if the profile says how: stock
    chat completions has no input slot for it."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        profile: str = "openai",
        base_url: str | None = None,
        client: openai.AsyncOpenAI | None = None,
        options: Mapping[str, Any] | None = None,
        models: Mapping[str, ModelConfig | Mapping[str, Any]] | None = None,
        max_tokens_param: str = "max_tokens",
        stream_usage: bool = True,
    ) -> None:
        if profile not in _PROFILES:
            raise ValueError(f"unknown profile {profile!r}; known: {', '.join(_PROFILES)}")
        self._profile = _PROFILES[profile]
        if client is None:
            client = _client(self._profile, api_key, base_url)
        self._client = client.with_options(max_retries=0)  # the runtime owns retries
        self._options = dict(options or {})
        self._models = merged_configs(self._profile.models, model_configs(models))
        self._max_tokens_param = max_tokens_param
        self._stream_usage = stream_usage

    async def list_models(self) -> Sequence[ModelInfo]:
        try:
            models = [m async for m in self._client.models.list()]
        except (openai.APIError, httpx2.TransportError) as e:
            raise _map_error(e) from e
        return [configured(ModelInfo(id=m.id, name=m.id, context_window=_extra_int(
                               m, _CONTEXT_WINDOW_FIELDS), max_output_tokens=_extra_int(
                               m, _MAX_OUTPUT_FIELDS)),
                           self._models.get(m.id))
                for m in models]

    async def stream(self, request: InferenceRequest) -> AsyncIterator[CoreEvent]:
        options = self._options
        if request.maintenance:
            options = {**self._profile.maintenance_options, **options}
        params: dict[str, Any] = {
            "model": request.model,
            "messages": to_messages(request.system, request.messages,
                                    reasoning_field=self._profile.reasoning_input_field,
                                    model=request.model, images=self._profile.images),
            "extra_body": request_options(options, self._models.get(request.model)),
        }
        if request.tools:
            params["tools"] = [to_tool(t) for t in request.tools]
        if self._stream_usage:
            params["stream_options"] = {"include_usage": True}
        if request.max_output_tokens is not None:
            params["extra_body"].setdefault(self._max_tokens_param, request.max_output_tokens)

        turn = _Turn(request.model)
        try:
            async with await self._client.chat.completions.create(**params, stream=True) as chunks:
                async for chunk in chunks:
                    for out in turn.feed(chunk):
                        yield out
        except (openai.APIError, httpx2.TransportError) as e:
            raise _map_error(e) from e
        if turn.finish_reason is None:
            raise TransientCoreError("stream ended without a finish_reason")
        if turn.finish_reason in self._profile.transient_finish_reasons:
            raise TransientCoreError(f"generation interrupted: {turn.finish_reason}")
        if turn.usage is None:
            payload = [to_messages(request.system, request.messages),
                       [to_tool(t) for t in request.tools]]
            turn.usage = Usage(
                input_tokens=_estimate(json.dumps(payload)),
                output_tokens=_estimate("".join(turn.text) + "".join(turn.reasoning)
                                        + "".join("".join(c.args) for c in turn.calls.values())),
            )
        yield turn.completed()

    async def aclose(self) -> None:
        await self._client.close()


def _client(profile: Profile, api_key: str | None, base_url: str | None) -> openai.AsyncOpenAI:
    api_key = api_key or os.environ.get(profile.api_key_env) or stored_key(profile.api_key_env)
    if not api_key and profile.api_key_required:
        raise ValueError(f"set {profile.api_key_env} or the api_key option")
    # Local servers usually need no key, but the SDK insists on one.
    api_key = api_key or "unused"
    if profile.base_url is None:
        return openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
    # The SDK would still read these from the environment and send them.
    return openai.AsyncOpenAI(
        api_key=api_key, base_url=base_url or profile.base_url,
        default_headers={"OpenAI-Organization": openai.Omit(), "OpenAI-Project": openai.Omit()})


def _extra_int(model: Any, names: Sequence[str]) -> int | None:
    extra = model.model_extra or {}
    for name in names:
        if isinstance(extra.get(name), int):
            return extra[name]
    return None


def _estimate(text: str) -> int:
    return math.ceil(len(text) / 4)


@dataclass
class _Call:
    id: str
    name: str | None = None
    args: list[str] = field(default_factory=list)
    announced: bool = False


class _Turn:
    def __init__(self, model: str) -> None:
        self.model = model
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[int, _Call] = {}
        self.finish_reason: str | None = None
        self.usage: Usage | None = None

    def feed(self, chunk: Any) -> list[CoreEvent]:
        out: list[CoreEvent] = []
        for choice in chunk.choices[:1]:
            delta = choice.delta
            extra = delta.model_extra or {}
            for name in _REASONING_FIELDS:
                if isinstance(extra.get(name), str) and extra[name]:
                    self.reasoning.append(extra[name])
                    out.append(ReasoningDelta(extra[name]))
                    break
            if delta.content:
                self.text.append(delta.content)
                out.append(TextDelta(delta.content))
            for tc in delta.tool_calls or ():
                out.extend(self._tool_delta(tc))
            if choice.finish_reason:
                self.finish_reason = choice.finish_reason
        if chunk.usage is not None:
            details = chunk.usage.prompt_tokens_details
            self.usage = Usage(
                input_tokens=chunk.usage.prompt_tokens,
                output_tokens=chunk.usage.completion_tokens,
                cached_tokens=(details.cached_tokens or 0) if details else 0,
            )
            out.append(UsageReport(self.usage))
        return out

    def _tool_delta(self, tc: Any) -> list[CoreEvent]:
        call = self.calls.get(tc.index)
        if call is None:
            # Some servers omit ids; generate one so results can be matched.
            call = self.calls[tc.index] = _Call(tc.id or f"call_{uuid.uuid4().hex[:24]}")
        fn = tc.function
        if fn and fn.name:
            call.name = fn.name
        chunk = (fn.arguments if fn else None) or ""
        call.args.append(chunk)
        if call.name is None:
            return []  # held back until the name is known
        if not call.announced:
            call.announced = True
            return [ToolCallDelta(call.id, call.name, "".join(call.args))]
        return [ToolCallDelta(call.id, call.name, chunk)] if chunk else []

    def completed(self) -> Completed:
        blocks: list[Block] = []
        if self.reasoning:
            blocks.append(Reasoning("".join(self.reasoning), model=self.model))
        if self.text:
            blocks.append(Text("".join(self.text)))
        for _, call in sorted(self.calls.items()):
            if call.name is None:
                raise CoreError("model sent a tool call without a name")
            arguments, raw = parse_arguments("".join(call.args))
            blocks.append(ToolCall(call.id, call.name, arguments, raw))
        stop = _STOP_REASONS.get(self.finish_reason or "", StopReason.END_TURN)
        if self.calls and stop in (StopReason.END_TURN, StopReason.REFUSAL):
            stop = StopReason.TOOL_USE
        return Completed(assistant_message(blocks), self.usage or Usage(), stop)


def to_tool(spec: ToolSpec) -> JSON:
    return {"type": "function", "function": {
        "name": spec.name, "description": spec.description, "parameters": spec.parameters}}


def to_messages(system: str, messages: Sequence[Message], *,
                reasoning_field: str | None = None, model: str | None = None,
                images: bool = True) -> list[JSON]:
    """With `reasoning_field`, every assistant entry carries it: the text of `model`'s own
    Reasoning blocks, or "" for turns without any (cut short, shrunk, another model's).
    A user's images go as image parts, or with `images` off as a line of text each."""
    out: list[JSON] = [{"role": "system", "content": system}] if system else []
    for message in messages:
        texts = [text for b in message.blocks if (text := _text(b))]
        calls = [b for b in message.blocks if isinstance(b, ToolCall)]
        returned: list[Image] = []  # pictures of tool results: a tool message is text only
        for result in (b for b in message.blocks if isinstance(b, ToolResult)):
            parts = [result_text(result, mark_errors=True)]
            for attached in result.attachments:
                if isinstance(attached, Image) and images:
                    returned.append(attached)
                else:
                    parts.append(attachment_text(attached))
            out.append({"role": "tool", "tool_call_id": result.call_id,
                        "content": "\n\n".join(parts)})
        if returned:
            out.append({"role": "user", "content": [
                {"type": "text", "text": "[What the tool returned with its result]"},
                *({"type": "image_url",
                   "image_url": {"url": f"data:{b.media_type};base64,{b.data}"}}
                  for b in returned)]})
        if message.role is Role.ASSISTANT:
            if not texts and not calls:
                continue
            entry: JSON = {"role": "assistant", "content": "\n\n".join(texts) or None}
            if reasoning_field:
                entry[reasoning_field] = "".join(
                    b.text for b in message.blocks
                    if isinstance(b, Reasoning) and b.model == model)
            if calls:
                entry["tool_calls"] = [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                    for c in calls
                ]
            out.append(entry)
        elif texts or any(isinstance(b, Image) for b in message.blocks):
            shown = [b for b in message.blocks if isinstance(b, Image)] if images else []
            texts += [attachment_text(b) for b in message.blocks
                      if isinstance(b, Image) and not images]
            text = "\n\n".join(texts)
            out.append({"role": "user", "content": text if not shown else [
                *([{"type": "text", "text": text}] if text else []),
                *({"type": "image_url",
                   "image_url": {"url": f"data:{b.media_type};base64,{b.data}"}}
                  for b in shown)]})
    return out


def _text(block: Block) -> str | None:
    match block:
        case Text(text=text):
            return text
        case OperationResult():
            return operation_text(block)
        case ServerToolUse():
            return server_tool_text(block)
        case File() | Document():
            return attachment_text(block)
    return None


def _map_error(error: Exception) -> CoreError:
    if isinstance(error, openai.APIStatusError):
        status = error.status_code
        message = f"{status}: {error.message}"
        code = str(getattr(error, "code", None) or "")
        if status in (400, 413) and (code in _OVERFLOW_CODES or any(
                m in str(error.message).lower() for m in _OVERFLOW_MESSAGES)):
            return ContextOverflowError(message)
        if status in (408, 409, 429) or status >= 500:
            return TransientCoreError(message, retry_after(error.response.headers))
        return CoreError(message)
    if isinstance(error, (openai.APIError, httpx2.TransportError)):
        # Connection failures, timeouts and errors sent mid-stream (no HTTP status).
        return TransientCoreError(f"{type(error).__name__}: {error}")
    return CoreError(f"{type(error).__name__}: {error}")
