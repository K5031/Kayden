"""Scripted Frontend that records every event envelope."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from kayden.api import (
    AgentState,
    EventEnvelope,
    Frontend,
    FrontendInfo,
    RuntimeAPI,
    RuntimeEvent,
    StateChanged,
)


class Recorder:
    """Subscribes on construction and records envelopes in the background."""

    def __init__(self, runtime: RuntimeAPI):
        self._events = runtime.events()
        self.envelopes: list[EventEnvelope] = []
        self.ended = False
        self._changed = asyncio.Condition()
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        async for envelope in self._events:
            async with self._changed:
                self.envelopes.append(envelope)
                self._changed.notify_all()
        async with self._changed:
            self.ended = True
            self._changed.notify_all()

    @property
    def events(self) -> list[RuntimeEvent]:
        return [e.event for e in self.envelopes]

    def of[T](self, kind: type[T]) -> list[T]:
        return [e for e in self.events if isinstance(e, kind)]

    @property
    def states(self) -> list[AgentState]:
        return [e.state for e in self.of(StateChanged)]

    @property
    def state(self) -> AgentState | None:
        states = self.states
        return states[-1] if states else None

    async def wait_for(self, predicate: Callable[[Recorder], Any], timeout: float = 5.0) -> Any:
        async def check() -> Any:
            async with self._changed:
                while not (result := predicate(self)):
                    await self._changed.wait()
                return result

        return await asyncio.wait_for(check(), timeout)

    async def wait_state(self, state: AgentState, *, count: int = 1, timeout: float = 5.0) -> None:
        await self.wait_for(lambda r: r.states.count(state) >= count, timeout)

    async def close(self) -> None:
        """Drain until the stream ends (after ShuttingDown), or give up after a moment."""
        await asyncio.wait({self._task}, timeout=1.0)
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


type Script = Callable[[RuntimeAPI, "ScriptedFrontend"], Awaitable[None]]


class ScriptedFrontend(Frontend):
    def __init__(self, script: Script | None = None, *, interactive: bool = True,
                 kind: str = "terminal", options: dict[str, Any] | None = None):
        self.script = script
        self._info = FrontendInfo("scripted", interactive_permissions=interactive, kind=kind)
        self.options = options or {}
        self.recorder: Recorder | None = None

    @property
    def info(self) -> FrontendInfo:
        return self._info

    def attach(self, runtime: RuntimeAPI) -> Recorder:
        self.recorder = Recorder(runtime)
        return self.recorder

    async def run(self, runtime: RuntimeAPI) -> None:
        self.attach(runtime)
        if self.script is not None:
            await self.script(runtime, self)

    async def aclose(self) -> None:
        if self.recorder is not None:
            await self.recorder.close()


def factory(options: dict[str, Any], paths: Any) -> ScriptedFrontend:
    """Options: script, interactive; any others are kept in `options`."""
    return ScriptedFrontend(options.get("script"), interactive=options.get("interactive", True),
                            options=options)


def window_factory(options: dict[str, Any], paths: Any) -> ScriptedFrontend:
    return ScriptedFrontend(options.get("script"), kind="window", options=options)


def other_window_factory(options: dict[str, Any], paths: Any) -> ScriptedFrontend:
    return ScriptedFrontend(options.get("script"), kind="window", options=options)


factory.display_name = "Scripted"  # type: ignore[attr-defined]
window_factory.display_name = "Scripted window"  # type: ignore[attr-defined]
window_factory.kind = "window"  # type: ignore[attr-defined]
other_window_factory.kind = "window"  # type: ignore[attr-defined]
