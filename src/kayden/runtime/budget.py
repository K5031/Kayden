"""Token estimation, usage accounting and budget enforcement."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from kayden.api import (
    Budget,
    Message,
    ModelInfo,
    OpaqueBlock,
    OperationResult,
    Reasoning,
    Text,
    TokenCounter,
    ToolCall,
    ToolResult,
    Usage,
    UsageSnapshot,
)

USAGE_SCHEMA_VERSION = 1
_CHARS_PER_TOKEN = 4
_MESSAGE_OVERHEAD = 4


def _text_tokens(text: str) -> int:
    return -(-len(text) // _CHARS_PER_TOKEN)


def _block_text(block: object) -> str:
    match block:
        case Text(text=t) | Reasoning(text=t):
            return t
        case ToolCall(name=n, arguments=a, raw_arguments=raw):
            return n + (raw if raw is not None else json.dumps(a))
        case ToolResult(content=c) | OperationResult(content=c):
            return c
        case OpaqueBlock(data=d):
            return json.dumps(d, default=str)
    return ""


def estimate_tokens(value: str | Sequence[Message]) -> int:
    """Fast local estimate: about four characters per token."""
    if isinstance(value, str):
        return _text_tokens(value)
    return sum(
        _MESSAGE_OVERHEAD + sum(_text_tokens(_block_text(b)) for b in m.blocks) for m in value
    )


def calibrated(ratio: float) -> TokenCounter:
    if ratio == 1.0:
        return estimate_tokens
    return lambda value: math.ceil(estimate_tokens(value) * ratio)


def cost_of(usage: Usage, model: ModelInfo | None) -> float:
    """Provider-reported cost when present, else computed from the model's prices."""
    if usage.cost_usd is not None:
        return usage.cost_usd
    if model is None or model.input_usd_per_mtok is None or model.output_usd_per_mtok is None:
        return 0.0
    cached_price = model.cached_input_usd_per_mtok
    if cached_price is None:
        cached_price = model.input_usd_per_mtok
    uncached = usage.input_tokens - usage.cached_tokens
    return (
        uncached * model.input_usd_per_mtok
        + usage.cached_tokens * cached_price
        + usage.output_tokens * model.output_usd_per_mtok
    ) / 1_000_000


def has_prices(model: ModelInfo) -> bool:
    return model.input_usd_per_mtok is not None and model.output_usd_per_mtok is not None


@dataclass
class Totals:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    elapsed_s: float = 0.0


def _read_totals(path: Path) -> Totals:
    data = json.loads(path.read_text())
    data.pop("schema_version", None)
    return Totals(**data)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


class BudgetTracker:
    """Usage for the instance lifetime, persisted as JSON in the data directory: written
    off the event loop, one write at a time, the latest totals each time (flush() waits).

    Duration accrues while at least one inference or tool operation is active."""

    def __init__(self, budget: Budget):
        self.budget = budget
        self.totals = Totals()
        self._path: Path | None = None
        self._active = 0
        self._active_since = 0.0
        self._dirty = False
        self._writer: asyncio.Task[None] | None = None

    def load(self, path: Path) -> None:
        self._path = path
        if path.exists():
            self.totals = _read_totals(path)

    def save(self) -> None:
        if self._path is None:
            return
        self._dirty = True
        if self._writer is None or self._writer.done():
            self._writer = asyncio.get_running_loop().create_task(self._write())

    async def flush(self) -> None:
        if self._writer is not None:
            await self._writer

    async def _write(self) -> None:
        while self._dirty and self._path is not None:
            self._dirty = False
            data = {"schema_version": USAGE_SCHEMA_VERSION, **asdict(self.totals)}
            data["elapsed_s"] = self.elapsed()
            await asyncio.to_thread(_write_json, self._path, data)

    # Duration

    def elapsed(self) -> float:
        running = time.monotonic() - self._active_since if self._active else 0.0
        return self.totals.elapsed_s + running

    def begin_activity(self) -> None:
        if self._active == 0:
            self._active_since = time.monotonic()
        self._active += 1

    def end_activity(self) -> None:
        self._active -= 1
        if self._active == 0:
            self.totals.elapsed_s += time.monotonic() - self._active_since
            self.save()

    def seconds_until_duration_exhausted(self) -> float | None:
        if self.budget.max_duration_s is None:
            return None
        return max(0.0, self.budget.max_duration_s - self.elapsed())

    # Checks

    def exhausted(self, extra: Usage | None = None, extra_cost: float = 0.0) -> str | None:
        """Name and detail of the first exhausted dimension, counting `extra` in-flight
        usage, or None."""
        b, t = self.budget, self.totals
        tokens = t.input_tokens + t.output_tokens
        if extra is not None:
            tokens += extra.input_tokens + extra.output_tokens
        checks = (
            ("max_tokens", b.max_tokens, tokens),
            ("max_cost_usd", b.max_cost_usd, t.cost_usd + extra_cost),
            ("max_duration_s", b.max_duration_s, self.elapsed()),
            ("max_tool_calls", b.max_tool_calls, t.tool_calls),
        )
        for name, limit, used in checks:
            if limit is not None and used >= limit:
                return f"{name} exhausted ({used:g} of {limit:g})"
        return None

    def reserve_tool_call(self) -> str | None:
        """Atomically check every dimension and count one tool call."""
        if reason := self.exhausted():
            return reason
        self.totals.tool_calls += 1
        self.save()
        return None

    def charge(self, usage: Usage, cost: float) -> None:
        self.totals.input_tokens += usage.input_tokens
        self.totals.output_tokens += usage.output_tokens
        self.totals.cached_tokens += usage.cached_tokens
        self.totals.cost_usd += cost
        self.save()

    def snapshot(self) -> UsageSnapshot:
        t, b = self.totals, self.budget
        elapsed = self.elapsed()
        used = {
            "max_tokens": t.input_tokens + t.output_tokens,
            "max_cost_usd": t.cost_usd,
            "max_duration_s": elapsed,
            "max_tool_calls": t.tool_calls,
        }
        remaining = {
            name: max(0.0, limit - used[name])
            for name, limit in asdict(b).items()
            if limit is not None
        }
        return UsageSnapshot(
            Usage(t.input_tokens, t.output_tokens, t.cached_tokens, t.cost_usd),
            t.tool_calls,
            elapsed,
            remaining,
        )
