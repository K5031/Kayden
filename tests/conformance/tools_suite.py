"""Reusable conformance suite for the Tools contract.

Usage: subclass ToolsConformance in a test module and provide two fixtures:

    class TestMyTools(ToolsConformance):
        @pytest.fixture
        def tools(self, tmp_path) -> Tools: ...

        @pytest.fixture
        def cases(self, tmp_path) -> ToolCases: ...

The suite cannot invent meaningful calls for an arbitrary module, so `cases` names a few
calls with known behavior. Child-process checks only see descendants of the test process,
which is exactly what a Tools module that "leaves no orphans" must clean up.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

import jsonschema
import psutil
import pytest

from kayden.api import (
    PERMISSION_CEILING_ENV,
    RESERVED_TOOL_NAMES,
    ToolCall,
    ToolOutput,
    Tools,
    ToolSpec,
)

MAX_LOOP_LAG = 0.1  # seconds; a tool that holds the loop longer is blocking it
CANCEL_DEADLINE = 3.0  # seconds from cancel() until execute() must have returned
REAP_DEADLINE = 3.0  # seconds after cancellation for child processes to be gone


@dataclass
class ToolCases:
    quick: ToolCall  # returns promptly with is_error=False
    failing: ToolCall  # fails in an expected way; must return is_error=True, not raise
    long_running: ToolCall  # runs >= 5 s, reports progress, may spawn child processes
    # Extra calls whose loop lag is measured (e.g. heavy file operations).
    extra: Sequence[ToolCall] = field(default_factory=tuple)
    # A call that launches a subprocess and returns PERMISSION_CEILING_ENV as it sees it;
    # None if the module launches no subprocesses.
    env_probe: ToolCall | None = None


@dataclass
class RecordingContext:
    """ToolContext test double. progress() is thread-safe, as the runtime's is; an empty
    text is a silent heartbeat."""

    call_id: str
    reports: list[tuple[float, str]] = field(default_factory=list)  # visible progress
    heartbeats: int = 0
    foreign_thread_calls: int = 0
    first_report: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._lock = threading.Lock()

    def progress(self, text: str) -> None:
        assert isinstance(text, str)
        with self._lock:
            if threading.get_ident() != self._loop_thread:
                self.foreign_thread_calls += 1
            if not text:
                self.heartbeats += 1
                return
            self.reports.append((time.monotonic(), text))
        if threading.get_ident() == self._loop_thread:
            self.first_report.set()
        else:
            self._loop.call_soon_threadsafe(self.first_report.set)


class LoopLagProbe:
    """Measures the worst delay of a 10 ms ticker while something else runs on the loop."""

    def __init__(self, interval: float = 0.01) -> None:
        self.interval = interval
        self.max_lag = 0.0
        self._task: asyncio.Task[None] | None = None

    async def _tick(self) -> None:
        while True:
            start = time.monotonic()
            await asyncio.sleep(self.interval)
            self.max_lag = max(self.max_lag, time.monotonic() - start - self.interval)

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._tick())
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


def _descendants() -> set[psutil.Process]:
    return set(psutil.Process().children(recursive=True))


def _alive(procs: set[psutil.Process]) -> set[psutil.Process]:
    alive = set()
    for proc in procs:
        try:
            if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                alive.add(proc)
        except psutil.NoSuchProcess:
            pass
    return alive


class ToolsConformance:
    @pytest.fixture
    def tools(self, tmp_path: Path) -> Tools:
        raise NotImplementedError("provide a `tools` fixture")

    @pytest.fixture
    def cases(self, tmp_path: Path) -> ToolCases:
        raise NotImplementedError("provide a `cases` fixture")

    @pytest.fixture
    def make_ctx(self, tmp_path: Path):
        return lambda call: RecordingContext(call.id)

    async def _specs(self, tools: Tools) -> dict[str, ToolSpec]:
        specs = await tools.list_tools()
        return {spec.name: spec for spec in specs}

    # ── declarations ─────────────────────────────────────────────────────

    async def test_specs_are_well_formed(self, tools: Tools) -> None:
        specs = list(await tools.list_tools())
        assert specs, "a Tools module should declare at least one tool"
        names = [spec.name for spec in specs]
        assert len(names) == len(set(names)), "tool names must be unique"
        for spec in specs:
            assert isinstance(spec, ToolSpec)
            assert spec.name not in RESERVED_TOOL_NAMES, f"{spec.name} is reserved"
            assert spec.name.strip() and spec.description.strip()
            jsonschema.validators.validator_for(spec.parameters).check_schema(spec.parameters)
            assert spec.parameters.get("type") == "object", f"{spec.name}: must be an object"
            assert spec.stall_timeout is None or spec.stall_timeout > 0
            assert spec.timeout is None or spec.timeout > 0  # math.inf = no limit

    async def test_case_arguments_match_schemas(self, tools: Tools, cases: ToolCases) -> None:
        specs = await self._specs(tools)
        probes = [cases.env_probe] if cases.env_probe else []
        for call in (cases.quick, cases.failing, cases.long_running, *cases.extra, *probes):
            assert call.name in specs, f"case uses undeclared tool {call.name}"
            jsonschema.validate(call.arguments, specs[call.name].parameters)

    # ── results and errors ───────────────────────────────────────────────

    async def test_quick_call_returns_output(self, tools, cases, make_ctx) -> None:
        out = await tools.execute(cases.quick, make_ctx(cases.quick))
        assert isinstance(out, ToolOutput)
        assert isinstance(out.content, str)
        assert not out.is_error, out.content

    async def test_failure_is_an_error_output(self, tools, cases, make_ctx) -> None:
        out = await tools.execute(cases.failing, make_ctx(cases.failing))
        assert isinstance(out, ToolOutput)
        assert out.is_error
        assert out.content, "errors should explain themselves to the model"

    async def test_confined_answers_for_every_call(self, tools: Tools, cases: ToolCases) -> None:
        for call in (cases.quick, cases.failing, cases.long_running, *cases.extra):
            assert isinstance(await tools.confined(call), bool), call.name
        assert await tools.confined(ToolCall("x", "no-such-tool", {})) is False

    async def test_unknown_tool_is_an_error_output(self, tools, make_ctx) -> None:
        call = ToolCall("unknown", "no_such_tool_xyz", {})
        out = await tools.execute(call, make_ctx(call))
        assert isinstance(out, ToolOutput) and out.is_error

    async def test_invalid_arguments_are_error_outputs(self, tools, make_ctx) -> None:
        for spec in await tools.list_tools():
            if not spec.parameters.get("required"):
                continue
            call = ToolCall(f"invalid-{spec.name}", spec.name, {})
            out = await tools.execute(call, make_ctx(call))
            assert isinstance(out, ToolOutput) and out.is_error, spec.name

    # ── progress ─────────────────────────────────────────────────────────

    async def test_long_running_reports_progress(self, tools, cases, make_ctx) -> None:
        spec = (await self._specs(tools))[cases.long_running.name]
        ctx = make_ctx(cases.long_running)
        task = asyncio.create_task(tools.execute(cases.long_running, ctx))
        try:
            window = min(spec.stall_timeout or 2.0, 2.0)
            await asyncio.wait_for(ctx.first_report.wait(), window)
            await asyncio.sleep(1.0)
            assert len(ctx.reports) >= 2, "progress should keep coming while work continues"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # ── environment ──────────────────────────────────────────────────────

    async def test_subprocesses_inherit_the_permission_ceiling(
        self, cases, tools, make_ctx, monkeypatch
    ) -> None:
        if cases.env_probe is None:
            pytest.skip("module launches no subprocesses")
        monkeypatch.setenv(PERMISSION_CEILING_ENV, "deny")
        out = await tools.execute(cases.env_probe, make_ctx(cases.env_probe))
        assert not out.is_error and "deny" in out.content

    # ── cancellation ─────────────────────────────────────────────────────

    async def test_cancellation_is_prompt_and_leaves_no_orphans(
        self, tools, cases, make_ctx
    ) -> None:
        before = _descendants()
        ctx = make_ctx(cases.long_running)
        task = asyncio.create_task(tools.execute(cases.long_running, ctx))
        await asyncio.wait_for(ctx.first_report.wait(), 5.0)
        await asyncio.sleep(0.2)
        spawned = _descendants() - before

        task.cancel()
        started = time.monotonic()
        done, _ = await asyncio.wait({task}, timeout=CANCEL_DEADLINE)
        assert task in done, f"execute() ignored cancellation for {CANCEL_DEADLINE}s"
        assert time.monotonic() - started < CANCEL_DEADLINE
        assert task.cancelled(), "CancelledError must propagate, not become an output"

        deadline = time.monotonic() + REAP_DEADLINE
        while (alive := _alive(spawned)) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert not alive, f"orphaned processes after cancellation: {alive}"

    # ── event loop ───────────────────────────────────────────────────────

    async def test_never_blocks_the_event_loop(self, tools, cases, make_ctx) -> None:
        async with LoopLagProbe() as probe:
            for call in (cases.quick, cases.failing, *cases.extra):
                await tools.execute(call, make_ctx(call))
            task = asyncio.create_task(
                tools.execute(cases.long_running, make_ctx(cases.long_running))
            )
            await asyncio.sleep(1.0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert probe.max_lag < MAX_LOOP_LAG, f"event loop blocked for {probe.max_lag:.3f}s"

    async def test_concurrent_calls(self, tools, cases, make_ctx) -> None:
        calls = [cases.quick, cases.failing, cases.quick]
        outs = await asyncio.gather(*(tools.execute(c, make_ctx(c)) for c in calls))
        assert [o.is_error for o in outs] == [False, True, False]

    async def test_aclose(self, tools) -> None:
        await tools.aclose()
