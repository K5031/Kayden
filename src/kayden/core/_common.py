"""Helpers shared by the provider adapters."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from email.utils import parsedate_to_datetime

from kayden.api import JSON, Block, CoreError, Message, Role, ToolResult, ToolStatus

_RUNTIME_STATUSES = {ToolStatus.CANCELLED, ToolStatus.TIMED_OUT, ToolStatus.INTERRUPTED}


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
    if result.status in _RUNTIME_STATUSES or (mark_errors and result.status is ToolStatus.ERROR):
        return f"[{result.status}] {result.content}"
    return result.content


def parse_arguments(raw: str, tool: str) -> JSON:
    if not raw.strip():
        return {}
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CoreError(f"model sent invalid JSON arguments for {tool!r}: {e}") from e
    if not isinstance(arguments, dict):
        raise CoreError(f"model sent non-object arguments for {tool!r}")
    return arguments


def assistant_message(blocks: list[Block]) -> Message:
    return Message(uuid.uuid4().hex, Role.ASSISTANT, tuple(blocks), time.time())
