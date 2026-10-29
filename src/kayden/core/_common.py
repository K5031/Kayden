"""Helpers shared by the provider adapters."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from email.utils import parsedate_to_datetime
from typing import Any

from kayden.api import (
    JSON,
    Block,
    Message,
    ModelInfo,
    OperationResult,
    Role,
    ToolResult,
    ToolStatus,
)

ERROR_STATUSES = frozenset(ToolStatus) - {ToolStatus.OK, ToolStatus.BACKGROUNDED}


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """Per-model adapter configuration. Anything left None comes from the provider."""

    options: Mapping[str, Any] = field(default_factory=dict)  # merged over adapter options
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    cached_input_usd_per_mtok: float | None = None


def model_configs(models: Mapping[str, ModelConfig | Mapping[str, Any]] | None
                  ) -> dict[str, ModelConfig]:
    return {model_id: c if isinstance(c, ModelConfig) else ModelConfig(**c)
            for model_id, c in (models or {}).items()}


def configured(info: ModelInfo, config: ModelConfig | None) -> ModelInfo:
    """`info` with configured values taking precedence over what the provider reported."""
    if config is None:
        return info
    return replace(
        info,
        context_window=config.context_window or info.context_window,
        max_output_tokens=config.max_output_tokens or info.max_output_tokens,
        input_usd_per_mtok=config.input_usd_per_mtok,
        output_usd_per_mtok=config.output_usd_per_mtok,
        cached_input_usd_per_mtok=config.cached_input_usd_per_mtok,
    )


def request_options(options: Mapping[str, Any], config: ModelConfig | None) -> JSON:
    return {**options, **(config.options if config else {})}


def retry_after(headers: Mapping[str, str] | None) -> float | None:
    """Seconds from `retry-after-ms` or `retry-after` (delta-seconds or HTTP date)."""
    if not headers:
        return None
    if (ms := headers.get("retry-after-ms")) is not None:
        try:
            return float(ms) / 1000
        except ValueError:
            pass
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def result_text(result: ToolResult, *, mark_errors: bool = False) -> str:
    """Tool result content; runtime-assigned statuses are made visible to the model.
    `mark_errors` also tags ERROR, for formats without an error flag."""
    if result.status is ToolStatus.OK or (result.status is ToolStatus.ERROR and not mark_errors):
        return result.content
    return f"[{result.status}] {result.content}"


def operation_text(result: OperationResult) -> str:
    return (f"[Background operation {result.call_id} finished: {result.status}]\n"
            f"{result.content}")


def parse_arguments(raw: str) -> tuple[JSON, str | None]:
    """(arguments, raw_arguments): raw_arguments is set, with empty arguments, when the
    model's JSON does not parse to an object."""
    if not raw.strip():
        return {}, None
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError:
        return {}, raw
    return (arguments, None) if isinstance(arguments, dict) else ({}, raw)


def assistant_message(blocks: list[Block]) -> Message:
    return Message(uuid.uuid4().hex, Role.ASSISTANT, tuple(blocks), time.time())
