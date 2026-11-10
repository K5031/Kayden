"""Fake Tools module with instant, slow, hanging, failing and thread-using tools."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from kayden.api import ToolCall, ToolContext, ToolOutput, Tools, ToolSpec

type ToolFn = Callable[[ToolCall, ToolContext], Awaitable[ToolOutput]]

ANY_ARGS = {"type": "object"}


class FakeTools(Tools):
    def __init__(self, trace: list[Any] | None = None):
        self.specs: dict[str, ToolSpec] = {}
        self.fns: dict[str, ToolFn] = {}
        self.trace = trace if trace is not None else []
        self.started: list[str] = []  # call ids in start order
        self.cancelled: list[str] = []
        self.finished: list[str] = []
        self.running = 0
        self.max_running = 0
        self.envs: list[dict[str, str]] = []
        self.release = asyncio.Event()  # lets cancel-ignoring tools finish
        self.list_error: Exception | None = None
        self.gates: dict[str, asyncio.Event] = {}
        self.options: dict[str, Any] = {}
        self.closed = False
        self.confined_names: set[str] = set()  # tools whose calls stay in the work folder
        self.folders: list[Path | None] | None = None  # use_folder calls; None = fixed folder
        self.folder_error: OSError | None = None
        self.received: list[ToolCall] = []  # calls as execute and confined got them

    def add(self, spec: ToolSpec, fn: ToolFn) -> ToolSpec:
        self.specs[spec.name] = spec
        self.fns[spec.name] = fn
        return spec

    # ── builders ──

    def instant(self, name: str, result: str = "done", *, read_only: bool = True,
                schema: dict[str, Any] | None = None) -> ToolSpec:
        async def fn(call: ToolCall, ctx: ToolContext) -> ToolOutput:
            return ToolOutput(f"{result}:{call.arguments}" if call.arguments else result)

        return self.add(ToolSpec(name, f"{name} tool", schema or ANY_ARGS, read_only), fn)

    def slow(self, name: str, seconds: float, *, read_only: bool = True, steps: int = 5,
             result: str | None = None, **spec_kw: Any) -> ToolSpec:
        async def fn(call: ToolCall, ctx: ToolContext) -> ToolOutput:
            for i in range(steps):
                await asyncio.sleep(seconds / steps)
                ctx.progress(f"{name} {i + 1}/{steps}")
            return ToolOutput(result or f"{name} finished")

        return self.add(ToolSpec(name, f"{name} tool", ANY_ARGS, read_only, **spec_kw), fn)

    def hanging(self, name: str, *, honor_cancel: bool = True, read_only: bool = True,
                progress: str | None = None, **spec_kw: Any) -> ToolSpec:
        async def fn(call: ToolCall, ctx: ToolContext) -> ToolOutput:
            if progress:
                ctx.progress(progress)
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    if honor_cancel:
                        raise
                    await self.release.wait()
                    return ToolOutput(f"{name} late result")

        return self.add(ToolSpec(name, f"{name} tool", ANY_ARGS, read_only, **spec_kw), fn)

    def gated(self, name: str, *, read_only: bool = True) -> ToolSpec:
        """Finishes when self.gates[name] is set."""
        gate = self.gates[name] = asyncio.Event()

        async def fn(call: ToolCall, ctx: ToolContext) -> ToolOutput:
            await gate.wait()
            return ToolOutput(f"{name} done")

        return self.add(ToolSpec(name, f"{name} tool", ANY_ARGS, read_only), fn)

    def failing(self, name: str, *, raises: bool = True, read_only: bool = True) -> ToolSpec:
        async def fn(call: ToolCall, ctx: ToolContext) -> ToolOutput:
            if raises:
                raise RuntimeError(f"{name} exploded")
            return ToolOutput(f"{name} failed", is_error=True)

        return self.add(ToolSpec(name, f"{name} tool", ANY_ARGS, read_only), fn)

    def threaded(self, name: str, seconds: float, *, read_only: bool = True) -> ToolSpec:
        async def fn(call: ToolCall, ctx: ToolContext) -> ToolOutput:
            stop = threading.Event()

            def work() -> str:
                deadline = time.monotonic() + seconds
                n = 0
                while time.monotonic() < deadline and not stop.is_set():
                    time.sleep(0.01)
                    n += 1
                    ctx.progress(f"tick {n}")
                return "stopped" if stop.is_set() else f"{name} finished"

            try:
                return ToolOutput(await asyncio.to_thread(work))
            except asyncio.CancelledError:
                stop.set()
                raise

        return self.add(ToolSpec(name, f"{name} tool", ANY_ARGS, read_only), fn)

    def env_probe(self, name: str = "env") -> ToolSpec:
        import os

        async def fn(call: ToolCall, ctx: ToolContext) -> ToolOutput:
            self.envs.append(dict(os.environ))
            return ToolOutput("env captured")

        return self.add(ToolSpec(name, "capture environment", ANY_ARGS, True), fn)

    # ── Tools contract ──

    async def aclose(self) -> None:
        self.closed = True

    async def list_tools(self) -> Sequence[ToolSpec]:
        if self.list_error is not None:
            raise self.list_error
        return list(self.specs.values())

    async def confined(self, call: ToolCall) -> bool:
        self.received.append(call)
        return call.name in self.confined_names

    async def use_folder(self, folder: Path | None) -> None:
        if self.folders is None:
            raise NotImplementedError
        if self.folder_error is not None:
            raise self.folder_error
        self.folders.append(folder)

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutput:
        self.received.append(call)
        self.started.append(call.id)
        self.trace.append(("tool_start", call.id))
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            return await self.fns[call.name](call, ctx)
        except asyncio.CancelledError:
            self.cancelled.append(call.id)
            raise
        finally:
            self.running -= 1
            self.finished.append(call.id)
            self.trace.append(("tool_end", call.id))


def factory(options: dict[str, Any], paths: Any) -> FakeTools:
    tools = FakeTools()
    tools.options = dict(options)
    for name in options.get("instant", ()):
        tools.instant(name)
    return tools
