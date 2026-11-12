"""Core for the Anthropic Messages API (official `anthropic` SDK)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
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
    server_tool_text,
    stored_key,
)

PROVIDER = "anthropic"
logger = logging.getLogger(__name__)
_THINKING = ("thinking", "redacted_thinking")
# Models that always think (Opus 5.x, Sonnet 5.x, Haiku 5.x, Fable, Mythos) hide their
# reasoning and their notes between tool calls unless asked: the stream goes quiet for the
# whole thought, then the answer arrives at once. Kayden asks for a summary of both.
_ALWAYS_THINKS = re.compile(r"claude-(?:(?:opus|sonnet|haiku)-5|fable|mythos)\b")
SHOWN_THINKING = {"type": "adaptive", "display": "summarized"}
# The 400 for a thinking block replayed after something before it changed.
_BINDING_ERROR = "bound to a different conversation"
_INVALID_ID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")  # ids from other providers may break the API
_TRANSIENT_ERROR_TYPES = {"rate_limit_error", "overloaded_error", "api_error", "timeout_error"}
_STOP_REASONS = {
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "model_context_window_exceeded": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
}
_OVERFLOW_MESSAGES = ("prompt is too long", "exceed context limit", "context window")

# Anthropic's own web search (Core.list_tools): run on Anthropic's side, $10 per 1 000
# searches. Models with dynamic filtering take the newer version.
WEB_SEARCH = ToolSpec(
    "web_search",
    "Search the web for anything recent or that you are not sure of. Anthropic runs the "
    "search; link the pages your answer relies on.",
    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    read_only=True,
)
MAX_SEARCHES = 5  # per request
SEARCH_USD = 0.01
# Automatic prompt caching: the API marks the end of each request, so the next one reads
# the conversation so far at the cached price and writes only what was added.
CACHED = {"type": "ephemeral"}
# What writing the cache costs on top of the input price, as a share of it, by lifetime.
_WRITE_PREMIUM = {"1h": 1.0}
_WRITE_PREMIUM_DEFAULT = 0.25  # five minutes
MAX_CONTINUATIONS = 5  # of a turn the API paused (pause_turn)
_DYNAMIC_FILTERING = ("claude-opus-5", "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7",
                      "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6")
_SEARCH_OFF = ("web search is not enabled", "web search is disabled")
_SHOWN = ("web_search", "web_fetch")  # server tools shown as steps; the filtering code is not


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


def _thinking_shown(model: str, body: dict[str, Any]) -> dict[str, Any]:
    """The request options, asking an always-thinking model to stream a summary of its
    thinking unless the configuration says otherwise about thinking."""
    if "thinking" in body or not _ALWAYS_THINKS.match(model):
        return body
    return {**body, "thinking": dict(SHOWN_THINKING)}


def _cached(body: dict[str, Any], request: InferenceRequest) -> dict[str, Any]:
    """The request options with the conversation cached, unless the configuration says
    how. The runtime's one-off requests are left alone: nothing reads what they write."""
    if "cache_control" in body or request.maintenance:
        return body
    return {**body, "cache_control": dict(CACHED)}


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
        self._search = True  # False once the account turned out to have it switched off

    async def list_tools(self) -> Sequence[ToolSpec]:
        return [WEB_SEARCH] if self._search else []

    def _tools(self, specs: Sequence[ToolSpec], model: str) -> list[JSON]:
        """The request's tools, web_search as Anthropic's own while it is on."""
        return [search_tool(model) if spec.name == WEB_SEARCH.name and self._search
                else to_tool(spec) for spec in specs]

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
        tools = self._tools(request.tools, request.model)
        turns, _ = to_messages(request.messages, request.model, request.system, tools,
                               search=self._search)
        params: dict[str, Any] = {"model": request.model, "messages": turns}
        if request.system:
            params["system"] = request.system
        if tools:
            params["tools"] = tools
        try:
            result = await self._client.messages.count_tokens(**params)
        except (anthropic.APIError, httpx2.TransportError) as e:
            raise _map_error(e) from e
        return result.input_tokens

    async def stream(self, request: InferenceRequest) -> AsyncIterator[CoreEvent]:
        tools = self._tools(request.tools, request.model)
        turns, bound = to_messages(request.messages, request.model, request.system, tools,
                                   search=self._search)
        params: dict[str, Any] = {
            "model": request.model,
            "messages": turns,
            "max_tokens": request.max_output_tokens or self._default_max_tokens,
            "extra_body": _cached(_thinking_shown(
                request.model, request_options(self._options, self._models.get(request.model))),
                request),
        }
        if request.system:
            params["system"] = request.system
        if tools:
            params["tools"] = tools

        turn = _Turn(request.model, bound, self._write_premium(request.model,
                                                               params["extra_body"]))
        try:
            for continuation in range(MAX_CONTINUATIONS + 1):
                response = await self._open(params, request)
                async with response as events:
                    async for event in events:
                        for out in turn.feed(event):
                            yield out
                if not turn.finished:
                    raise TransientCoreError("stream ended before message_stop")
                if turn.stop_reason != "pause_turn" or continuation == MAX_CONTINUATIONS:
                    break
                # The API paused a long server-side turn: send it back to be continued.
                if continuation == 0:
                    turns = params["messages"]
                params["messages"] = [*turns, {"role": "assistant", "content": turn.content()}]
                turn.next_response()
        except (anthropic.APIError, httpx2.TransportError) as e:
            raise _map_error(e) from e
        yield turn.completed()

    def _write_premium(self, model: str, body: dict[str, Any]) -> float:
        """USD per token written to the cache, beyond the input price it is counted at."""
        config = self._models.get(model)
        control = body.get("cache_control")
        if config is None or config.input_usd_per_mtok is None or not isinstance(control, dict):
            return 0.0
        share = _WRITE_PREMIUM.get(control.get("ttl", ""), _WRITE_PREMIUM_DEFAULT)
        return config.input_usd_per_mtok * share / 1_000_000

    async def _open(self, params: dict[str, Any], request: InferenceRequest) -> Any:
        """Start the stream; on a 400 the request can do without, again without it."""
        try:
            return await self._client.messages.create(**params, stream=True)
        except anthropic.BadRequestError as e:
            text = str(e)
            if self._search and any(m in text.lower() for m in _SEARCH_OFF):
                logger.warning("web search is switched off for this account; using Kayden's")
                self._search = False
            elif _BINDING_ERROR in text:
                # The API saw a change that Prefix did not: answer without earlier reasoning.
                logger.warning("thinking blocks rejected, sending the request without them: "
                               "%s", e)
            elif _replays_server_blocks(params["messages"]):
                # Earlier searches the API will not take back: send them as text instead.
                logger.warning("earlier server tool blocks rejected, replaying them as text: "
                               "%s", e)
            else:
                raise
            tools = self._tools(request.tools, request.model)
            if tools:
                params["tools"] = tools
            params["messages"], _ = to_messages(
                request.messages, request.model, request.system, tools,
                replay=_BINDING_ERROR not in text,
                search=self._search and not _replays_server_blocks(params["messages"]))
            return await self._client.messages.create(**params, stream=True)

    async def aclose(self) -> None:
        await self._client.close()


@dataclass
class _Block:
    type: str
    raw: JSON  # the block as the API sent it, deltas applied: what a continuation resends
    parts: list[str] = field(default_factory=list)  # tool input JSON as it streamed
    id: str = ""
    name: str = ""


class _Turn:
    """Accumulates raw stream events into Kayden events and the final message. A turn the
    API paused goes on in further responses (next_response), whose blocks follow."""

    def __init__(self, model: str, bound: str, write_premium_usd: float = 0.0) -> None:
        self.model = model
        self.bound = bound  # Prefix of the request: what this turn's reasoning is bound to
        self.write_premium_usd = write_premium_usd  # per token written to the cache
        self.blocks: list[_Block] = []
        self._index: dict[int, _Block] = {}  # this response's block indices
        self.counts = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_input_tokens",
                                     "cache_creation_input_tokens", "web_search_requests"), 0)
        self._earlier = dict(self.counts)  # what earlier responses of the turn reported
        self.stop_reason: str | None = None
        self.finished = False

    def next_response(self) -> None:
        self._earlier = {k: self._earlier[k] + v for k, v in self.counts.items()}
        self.counts = dict.fromkeys(self.counts, 0)
        self._index = {}
        self.stop_reason, self.finished = None, False

    def feed(self, event: Any) -> list[CoreEvent]:
        match event.type:
            case "message_start":
                return [self._usage(event.message.usage)]
            case "content_block_start":
                return self._start(event.index, event.content_block)
            case "content_block_delta":
                return self._delta(self._index[event.index], event.delta)
            case "message_delta":
                self.stop_reason = event.delta.stop_reason
                return [self._usage(event.usage)]
            case "message_stop":
                self.finished = True
        return []

    def _start(self, index: int, cb: Any) -> list[CoreEvent]:
        b = _Block(cb.type, cb.model_dump(exclude_none=True), id=getattr(cb, "id", "") or "",
                   name=getattr(cb, "name", "") or "")
        self._index[index] = b
        self.blocks.append(b)
        match cb.type:
            case "text" if cb.text:
                return [TextDelta(cb.text)]
            case "thinking" if cb.thinking:
                return [ReasoningDelta(cb.thinking)]
            case "tool_use" | "server_tool_use":  # a server tool shows as a step at once
                return [ToolCallDelta(b.id, b.name, "")]
        return []

    def _delta(self, b: _Block, delta: Any) -> list[CoreEvent]:
        match delta.type:
            case "text_delta":
                b.raw["text"] = b.raw.get("text", "") + delta.text
                return [TextDelta(delta.text)]
            case "thinking_delta":
                b.raw["thinking"] = b.raw.get("thinking", "") + delta.thinking
                return [ReasoningDelta(delta.thinking)]
            case "signature_delta":
                b.raw["signature"] = delta.signature
            case "citations_delta":
                b.raw.setdefault("citations", []).append(
                    delta.citation.model_dump(exclude_none=True))
            case "input_json_delta" if delta.partial_json:
                b.parts.append(delta.partial_json)
                return [ToolCallDelta(b.id, b.name, delta.partial_json)]
        return []

    def _usage(self, usage: Any) -> UsageReport:
        for key in self.counts:
            if (value := getattr(usage, key, None)) is not None:
                self.counts[key] = value
        server = getattr(usage, "server_tool_use", None)
        if server is not None and getattr(server, "web_search_requests", None) is not None:
            self.counts["web_search_requests"] = server.web_search_requests
        return UsageReport(self.usage())

    def usage(self) -> Usage:
        c = {k: self._earlier[k] + v for k, v in self.counts.items()}
        return Usage(
            input_tokens=(c["input_tokens"] + c["cache_read_input_tokens"]
                          + c["cache_creation_input_tokens"]),
            output_tokens=c["output_tokens"],
            cached_tokens=c["cache_read_input_tokens"],
            extra_cost_usd=(c["web_search_requests"] * SEARCH_USD
                            + c["cache_creation_input_tokens"] * self.write_premium_usd),
        )

    def content(self) -> list[JSON]:
        """The blocks so far as the API takes them back, tool inputs complete."""
        for b in self.blocks:
            if b.parts:
                b.raw["input"], _ = parse_arguments("".join(b.parts))
        return [b.raw for b in self.blocks]

    def completed(self) -> Completed:
        blocks: list[Block] = []
        uses: dict[str, JSON] = {}  # server tool uses waiting for their result
        sources: dict[str, str] = {}  # cited page addresses -> titles, in citing order
        # Every server-side block of the turn, in the order it came: they refer to each
        # other (dynamic filtering runs code that calls the search), so they are replayed
        # together, from the first use shown.
        server = [b.raw for b in self.blocks
                  if b.type == "server_tool_use" or b.type.endswith("_tool_result")
                  and "tool_use_id" in b.raw and b.type != "tool_result"]
        self.content()
        for b in self.blocks:
            raw = b.raw
            match raw["type"]:
                case "text":
                    blocks.append(Text(raw["text"]))
                    for citation in raw.get("citations", ()):
                        if url := citation.get("url"):
                            sources.setdefault(url, citation.get("title") or url)
                case "thinking":
                    state = {"provider": PROVIDER, "signature": raw.get("signature", ""),
                             "prefix": self.bound}
                    blocks.append(Reasoning(raw.get("thinking", ""), state, self.model))
                case "redacted_thinking":
                    state = {"provider": PROVIDER, "redacted": raw["data"],
                             "prefix": self.bound}
                    blocks.append(Reasoning("", state, self.model))
                case "tool_use":
                    arguments, bad = (parse_arguments("".join(b.parts)) if b.parts
                                      else (raw.get("input", {}), None))
                    blocks.append(ToolCall(raw["id"], raw["name"], arguments, bad))
                case "server_tool_use":
                    uses[raw["id"]] = raw
                case kind if kind.endswith("_tool_result") and raw.get("tool_use_id") in uses:
                    use = uses.pop(raw["tool_use_id"])
                    if use["name"] in _SHOWN or not any(isinstance(x, ServerToolUse)
                                                        for x in blocks):
                        blocks.append(_server_use(use, raw, self.model))
        shown = [i for i, x in enumerate(blocks) if isinstance(x, ServerToolUse)]
        for n, i in enumerate(shown):
            state = {"provider": PROVIDER, "model": self.model, "blocks": server if n == 0 else []}
            blocks[i] = replace(blocks[i], state=state)
        if sources:
            links = " · ".join(f"[{title}]({url})" for url, title in sources.items())
            blocks.append(Text(f"\n\nSources: {links}"))
        stop = _STOP_REASONS.get(self.stop_reason or "", StopReason.END_TURN)
        return Completed(assistant_message(blocks), self.usage(), stop)


def _server_use(use: JSON, result: JSON, model: str) -> ServerToolUse:
    """A tool Anthropic ran, from its use and result blocks, which are kept for replay."""
    name, query = use["name"], use.get("input", {})
    content = result.get("content")
    error = content.get("error_code") if isinstance(content, dict) else None
    if name == "web_search":
        summary = f"Searched the web for “{query.get('query', '')}”"
    elif name == "web_fetch":
        summary = f"Read {query.get('url', 'a page')}"
    elif "code_execution" in name:
        summary = "Ran code on Anthropic's side to sift the results"
    else:
        summary = f"Used {name} on Anthropic's side"
    if error is not None:
        readable = f"Failed: {error}"
    elif isinstance(content, list):
        readable = "\n".join(f"{item.get('title', '')}\n{item['url']}" for item in content
                             if isinstance(item, dict) and item.get("url"))
    elif isinstance(content, dict):
        readable = "\n".join(str(content[k]) for k in ("stdout", "stderr") if content.get(k))
    else:
        readable = ""
    return ServerToolUse(use["id"], name, query, summary, readable[:4000], error is not None)


def _replays_server_blocks(turns: Sequence[JSON]) -> bool:
    return any(c.get("type") == "server_tool_use" for t in turns for c in t["content"])


def search_tool(model: str) -> JSON:
    """Anthropic's web search for `model`, at most MAX_SEARCHES uses per request."""
    version = ("web_search_20260209" if model.startswith(_DYNAMIC_FILTERING)
               else "web_search_20250305")
    return {"type": version, "name": WEB_SEARCH.name, "max_uses": MAX_SEARCHES}


def to_tool(spec: ToolSpec) -> JSON:
    return {"name": spec.name, "description": spec.description, "input_schema": spec.parameters}


class Prefix:
    """Fingerprint of what a thinking block is bound to: the system prompt, the tools and
    every turn before it, earlier thinking excepted. The API rejects a block replayed after
    any of them changed, so its fingerprint is kept with it and compared before replaying."""

    def __init__(self, system: str, tools: Sequence[JSON]) -> None:
        definitions = sorted(json.dumps(t, sort_keys=True) for t in tools)
        self._hash = hashlib.sha256(json.dumps([system, definitions]).encode())
        self._turns = 0

    def of(self, turns: Sequence[JSON]) -> str:
        """The fingerprint before the turn that follows `turns`; takes a growing list."""
        for turn in turns[self._turns:]:
            content = [c for c in turn["content"] if c["type"] not in _THINKING]
            self._hash.update(json.dumps([turn["role"], content], sort_keys=True).encode())
        self._turns = len(turns)
        return self._hash.hexdigest()[:16]


def to_messages(messages: Sequence[Message], model: str, system: str = "",
                tools: Sequence[JSON] = (), *, replay: bool = True, search: bool = True
                ) -> tuple[list[JSON], str]:
    """Kayden messages to Anthropic turns, and the Prefix the answer's reasoning is bound
    to. Tool results ride in user turns, and adjacent same-role turns are merged with tool
    results first, as the API requires. Reasoning is replayed only to the model that
    produced it and while its Prefix is unchanged; never with `replay` off. A tool Anthropic
    ran is replayed as it came, to the model that ran it while `search` is on, only after
    the user's latest message: its results hold the pages' text, which every later request
    would pay for again. Earlier ones, and the rest, go as text. `tools` are the request's
    tool definitions."""
    out: list[JSON] = []
    prefix = Prefix(system, tools)
    latest = max((i for i, m in enumerate(messages) if m.role is Role.USER
                  and any(isinstance(b, Text) for b in m.blocks)), default=-1)
    for i, message in enumerate(messages):
        role = "assistant" if message.role is Role.ASSISTANT else "user"
        bound = prefix.of(out) if replay and role == "assistant" else None
        content = [c for b in message.blocks
                   for c in _to_content(b, model, bound, search and i > latest)]
        if not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(content)
        else:
            out.append({"role": role, "content": content})
        if role == "user":  # tool results first, then documents, before the text about them
            out[-1]["content"].sort(key=lambda c: _USER_ORDER.get(c["type"], 2))
    return out, prefix.of(out)


_USER_ORDER = {"tool_result": 0, "document": 1}


def _to_content(block: Block, model: str, bound: str | None, search: bool) -> list[JSON]:
    match block:
        case Text(text=text):
            return [{"type": "text", "text": text}] if text else []
        case Reasoning(model=produced_by) if produced_by != model:
            return []  # another model's signature would be rejected
        case Reasoning(state=state) if bound is None or (state or {}).get("prefix") != bound:
            return []  # so would one bound to a different system prompt, tools or history
        case Reasoning(text=text, state={"provider": "anthropic", "signature": signature}):
            return [{"type": "thinking", "thinking": text, "signature": signature}]
        case Reasoning(state={"provider": "anthropic", "redacted": data}):
            return [{"type": "redacted_thinking", "data": data}]
        case Reasoning():
            return []  # unsigned reasoning cannot be replayed
        case ServerToolUse(state={"provider": "anthropic", "model": ran_on, "blocks": raw}) \
                if search and ran_on == model:
            return list(raw)  # the turn's server blocks ride on its first shown use
        case ServerToolUse():
            return [{"type": "text", "text": server_tool_text(block)}]
        case ToolCall(id=id, name=name, arguments=arguments):
            return [{"type": "tool_use", "id": _INVALID_ID_CHARS.sub("_", id), "name": name,
                     "input": arguments}]
        case ToolResult(call_id=call_id, status=status, attachments=attached):
            content: str | list[JSON] = result_text(block)
            if attached:  # what the tool returned to look at, inside its result
                content = [{"type": "text", "text": content},
                           *(c for a in attached for c in _to_content(a, model, bound, search))]
            return [{"type": "tool_result",
                     "tool_use_id": _INVALID_ID_CHARS.sub("_", call_id),
                     "content": content, "is_error": status in ERROR_STATUSES}]
        case OperationResult():
            return [{"type": "text", "text": operation_text(block)}]
        case Image(media_type=media_type, data=data):
            return [{"type": "image",
                     "source": {"type": "base64", "media_type": media_type, "data": data}}]
        case File():
            return [{"type": "text", "text": attachment_text(block)}]
        case Document(data=data):
            return [{"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf", "data": data}}]
    return []  # OpaqueBlock


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
