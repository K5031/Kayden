"""Core for the Anthropic Messages API (official `anthropic` SDK)."""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx2

from kayden.api import (
    JSON,
    Block,
    Completed,
    ContextOverflowError,
    Core,
    CoreError,
    CoreEvent,
    InferenceRequest,
    InstancePaths,
    Message,
    ModelInfo,
    OperationResult,
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
    TransientCoreError,
    Usage,
    UsageReport,
)
from kayden.core._common import (
    ERROR_STATUSES,
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
    stored_key,
)

PROVIDER = "anthropic"
_INVALID_ID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")  # ids from other providers may break the API
_TRANSIENT_ERROR_TYPES = {"rate_limit_error", "overloaded_error", "api_error", "timeout_error"}
_STOP_REASONS = {
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "model_context_window_exceeded": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
}
_OVERFLOW_MESSAGES = ("prompt is too long", "exceed context limit", "context window")


# USD per million tokens, from Anthropic's price list of 2026-10-06; the API does not
# report prices. Configured `models` entries override them field by field. Haiku 5.5 uses
# its rate for prompts over 100K tokens, so the cost budget errs high. A missing cached
# price falls back to the input price.
_PRICES = {
    model_id: ModelConfig(input_usd_per_mtok=i, output_usd_per_mtok=o,
                          cached_input_usd_per_mtok=c)
    for model_id, i, o, c in (
        ("claude-fable-5-1", 10.0, 50.0, 0.25),
        ("claude-fable-5", 10.0, 50.0, None),
        ("claude-opus-5-5", 4.0, 20.0, 0.20),
        ("claude-opus-5", 5.0, 25.0, None),
        ("claude-opus-4-8", 5.0, 25.0, None),
        ("claude-opus-4-7", 5.0, 25.0, None),
        ("claude-opus-4-6", 5.0, 25.0, None),
        ("claude-sonnet-5-5", 2.0, 10.0, 0.20),
        ("claude-sonnet-5", 2.0, 10.0, None),
        ("claude-sonnet-4-6", 3.0, 15.0, None),
        ("claude-haiku-5-5", 0.50, 2.50, None),
        ("claude-haiku-4-5-20251001", 1.0, 5.0, None),
    )
}


def factory(options: dict[str, Any], paths: InstancePaths) -> AnthropicCore:
    """Options: api_key (default ANTHROPIC_API_KEY), base_url, options, models,
    default_max_tokens; see AnthropicCore."""
    return AnthropicCore(**options)


factory.display_name = "Claude"  # type: ignore[attr-defined]
factory.key_name = "ANTHROPIC_API_KEY"  # type: ignore[attr-defined]


class AnthropicCore(Core):
    """`options` are merged verbatim into every request body (thinking, temperature,
    tool_choice, ...); `models` adds per-model options, context windows and prices."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
        options: Mapping[str, Any] | None = None,
        models: Mapping[str, ModelConfig | Mapping[str, Any]] | None = None,
        default_max_tokens: int = 8192,
    ) -> None:
        if client is None and not (api_key or os.environ.get("ANTHROPIC_API_KEY")
                                   or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            api_key = stored_key("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY is not set")
        client = client or anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
        self._client = client.with_options(max_retries=0)  # the runtime owns retries
        self._options = dict(options or {})
        self._models = merged_configs(_PRICES, model_configs(models))
        self._default_max_tokens = default_max_tokens

    async def list_models(self) -> Sequence[ModelInfo]:
        try:
            return [
                configured(ModelInfo(
                    id=m.id,
                    name=m.display_name,
                    context_window=getattr(m, "max_input_tokens", None),
                    max_output_tokens=getattr(m, "max_tokens", None),
                ), self._models.get(m.id))
                async for m in self._client.models.list()
                if getattr(m, "lifecycle", None) != "retired"
            ]
        except (anthropic.APIError, httpx2.TransportError) as e:
            raise _map_error(e) from e

    async def count_tokens(self, request: InferenceRequest) -> int:
        params: dict[str, Any] = {"model": request.model,
                                  "messages": to_messages(request.messages, request.model)}
        if request.system:
            params["system"] = request.system
        if request.tools:
            params["tools"] = [to_tool(t) for t in request.tools]
        try:
            result = await self._client.messages.count_tokens(**params)
        except (anthropic.APIError, httpx2.TransportError) as e:
            raise _map_error(e) from e
        return result.input_tokens

    async def stream(self, request: InferenceRequest) -> AsyncIterator[CoreEvent]:
        params: dict[str, Any] = {
            "model": request.model,
            "messages": to_messages(request.messages, request.model),
            "max_tokens": request.max_output_tokens or self._default_max_tokens,
            "extra_body": request_options(self._options, self._models.get(request.model)),
        }
        if request.system:
            params["system"] = request.system
        if request.tools:
            params["tools"] = [to_tool(t) for t in request.tools]

        turn = _Turn(request.model)
        try:
            async with await self._client.messages.create(**params, stream=True) as events:
                async for event in events:
                    for out in turn.feed(event):
                        yield out
        except (anthropic.APIError, httpx2.TransportError) as e:
            raise _map_error(e) from e
        if not turn.finished:
            raise TransientCoreError("stream ended before message_stop")
        yield turn.completed()

    async def aclose(self) -> None:
        await self._client.close()


@dataclass
class _Block:
    type: str
    parts: list[str] = field(default_factory=list)
    signature: str = ""
    data: str = ""
    id: str = ""
    name: str = ""


class _Turn:
    """Accumulates raw stream events into Kayden events and the final message."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.blocks: dict[int, _Block] = {}
        self.counts = {"input_tokens": 0, "output_tokens": 0,
                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        self.stop_reason: str | None = None
        self.finished = False

    def feed(self, event: Any) -> list[CoreEvent]:
        match event.type:
            case "message_start":
                return [self._usage(event.message.usage)]
            case "content_block_start":
                return self._start(event.index, event.content_block)
            case "content_block_delta":
                return self._delta(self.blocks[event.index], event.delta)
            case "message_delta":
                self.stop_reason = event.delta.stop_reason
                return [self._usage(event.usage)]
            case "message_stop":
                self.finished = True
        return []

    def _start(self, index: int, cb: Any) -> list[CoreEvent]:
        match cb.type:
            case "text":
                self.blocks[index] = _Block("text", [cb.text])
                return [TextDelta(cb.text)] if cb.text else []
            case "thinking":
                self.blocks[index] = _Block("thinking", [cb.thinking], signature=cb.signature)
                return [ReasoningDelta(cb.thinking)] if cb.thinking else []
            case "redacted_thinking":
                self.blocks[index] = _Block("redacted_thinking", data=cb.data)
            case "tool_use":
                b = self.blocks[index] = _Block("tool_use", id=cb.id, name=cb.name)
                return [ToolCallDelta(b.id, b.name, "")]
            case other:
                self.blocks[index] = _Block(other)  # server-side blocks: not part of v1
        return []

    def _delta(self, b: _Block, delta: Any) -> list[CoreEvent]:
        match delta.type:
            case "text_delta":
                b.parts.append(delta.text)
                return [TextDelta(delta.text)]
            case "thinking_delta":
                b.parts.append(delta.thinking)
                return [ReasoningDelta(delta.thinking)]
            case "signature_delta":
                b.signature = delta.signature
            case "input_json_delta" if delta.partial_json:
                b.parts.append(delta.partial_json)
                return [ToolCallDelta(b.id, b.name, delta.partial_json)]
        return []

    def _usage(self, usage: Any) -> UsageReport:
        for key in self.counts:
            if (value := getattr(usage, key, None)) is not None:
                self.counts[key] = value
        return UsageReport(self.usage())

    def usage(self) -> Usage:
        c = self.counts
        return Usage(
            input_tokens=(c["input_tokens"] + c["cache_read_input_tokens"]
                          + c["cache_creation_input_tokens"]),
            output_tokens=c["output_tokens"],
            cached_tokens=c["cache_read_input_tokens"],
        )

    def completed(self) -> Completed:
        blocks: list[Block] = []
        for _, b in sorted(self.blocks.items()):
            match b.type:
                case "text":
                    blocks.append(Text("".join(b.parts)))
                case "thinking":
                    state = {"provider": PROVIDER, "signature": b.signature}
                    blocks.append(Reasoning("".join(b.parts), state, self.model))
                case "redacted_thinking":
                    state = {"provider": PROVIDER, "redacted": b.data}
                    blocks.append(Reasoning("", state, self.model))
                case "tool_use":
                    arguments, raw = parse_arguments("".join(b.parts))
                    blocks.append(ToolCall(b.id, b.name, arguments, raw))
        stop = _STOP_REASONS.get(self.stop_reason or "", StopReason.END_TURN)
        return Completed(assistant_message(blocks), self.usage(), stop)


def to_tool(spec: ToolSpec) -> JSON:
    return {"name": spec.name, "description": spec.description, "input_schema": spec.parameters}


def to_messages(messages: Sequence[Message], model: str) -> list[JSON]:
    """Kayden messages to Anthropic turns: tool results ride in user turns, and adjacent
    same-role turns are merged with tool results first, as the API requires. Reasoning is
    replayed only to the model that produced it."""
    out: list[JSON] = []
    for message in messages:
        role = "assistant" if message.role is Role.ASSISTANT else "user"
        content = [c for b in message.blocks if (c := _to_content(b, model)) is not None]
        if not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(content)
        else:
            out.append({"role": role, "content": content})
        if role == "user":
            out[-1]["content"].sort(key=lambda c: c["type"] != "tool_result")
    return out


def _to_content(block: Block, model: str) -> JSON | None:
    match block:
        case Text(text=text):
            return {"type": "text", "text": text} if text else None
        case Reasoning(model=produced_by) if produced_by != model:
            return None  # another model's signature would be rejected
        case Reasoning(text=text, state={"provider": "anthropic", "signature": signature}):
            return {"type": "thinking", "thinking": text, "signature": signature}
        case Reasoning(state={"provider": "anthropic", "redacted": data}):
            return {"type": "redacted_thinking", "data": data}
        case Reasoning():
            return None  # unsigned reasoning cannot be replayed
        case ToolCall(id=id, name=name, arguments=arguments):
            return {"type": "tool_use", "id": _INVALID_ID_CHARS.sub("_", id), "name": name,
                    "input": arguments}
        case ToolResult(call_id=call_id, status=status):
            return {"type": "tool_result", "tool_use_id": _INVALID_ID_CHARS.sub("_", call_id),
                    "content": result_text(block), "is_error": status in ERROR_STATUSES}
        case OperationResult():
            return {"type": "text", "text": operation_text(block)}
    return None  # OpaqueBlock


def _map_error(error: Exception) -> CoreError:
    if isinstance(error, anthropic.APIStatusError):
        body = error.body if isinstance(error.body, dict) else {}
        detail = body.get("error") if isinstance(body.get("error"), dict) else {}
        status = error.status_code
        transient = (status in (408, 409, 429) or status >= 500
                     or detail.get("type") in _TRANSIENT_ERROR_TYPES)
        text = str(detail.get("message", error.message))
        message = f"{status} {detail.get('type', '')}: {text}"
        if status in (400, 413) and any(m in text.lower() for m in _OVERFLOW_MESSAGES):
            return ContextOverflowError(message)
        if transient:
            return TransientCoreError(message, retry_after(error.response.headers))
        return CoreError(message)
    if isinstance(error, (anthropic.APIConnectionError, httpx2.TransportError)):
        return TransientCoreError(f"{type(error).__name__}: {error}")
    return CoreError(f"{type(error).__name__}: {error}")
