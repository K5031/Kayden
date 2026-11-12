"""A minimal Frontend: type a line, read the answer. No colours, no history, no settings.

Use it with `[frontend] factory = "examples.line_frontend:factory"` in config.toml.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Callable
from typing import Any

from kayden.api import (
    AgentState,
    Decision,
    Failure,
    Frontend,
    FrontendInfo,
    InstancePaths,
    MessageCommitted,
    PermissionRequested,
    Role,
    RuntimeAPI,
    StartOptions,
    StateChanged,
    Text,
    ToolStarted,
)


async def stdin_lines() -> AsyncIterator[str]:
    """Lines typed in the terminal, read in a thread so the event loop stays free."""
    while line := await asyncio.to_thread(sys.stdin.readline):
        yield line.rstrip("\n")


class LineFrontend(Frontend):
    def __init__(self, lines: Callable[[], AsyncIterator[str]] = stdin_lines,
                 show: Callable[[str], None] = print) -> None:
        self._lines, self._show = lines, show

    @property
    def info(self) -> FrontendInfo:
        return FrontendInfo("lines")

    async def run(self, runtime: RuntimeAPI) -> None:
        """Returning from here quits Kayden."""
        await runtime.start(StartOptions())  # a new conversation
        idle = asyncio.Event()
        idle.set()
        printer = asyncio.create_task(self._print(runtime, idle))
        try:
            async for line in self._lines():
                if line in ("/quit", "/exit"):
                    break
                if line:
                    idle.clear()
                    await runtime.send(line)
                    await idle.wait()  # one message at a time keeps this example short
        finally:
            printer.cancel()

    async def _print(self, runtime: RuntimeAPI, idle: asyncio.Event) -> None:
        """Everything the runtime does arrives as an event."""
        async for envelope in runtime.events():
            match envelope.event:
                case MessageCommitted(message=message) if message.role is Role.ASSISTANT:
                    for block in message.blocks:
                        if isinstance(block, Text) and block.text:
                            self._show(block.text)
                case ToolStarted(call=call):
                    self._show(f"[{call.name} {call.arguments}]")
                case PermissionRequested(request=request):
                    self._show(f"[not allowed here: {request.call.name}]")
                    await runtime.resolve_permission(request.id, Decision.DENY)
                case Failure(message=message):
                    self._show(f"[failed: {message}]")
                    idle.set()
                case StateChanged(state=AgentState.IDLE | AgentState.STOPPED | AgentState.FAILED):
                    idle.set()


def factory(options: dict[str, Any], paths: InstancePaths) -> LineFrontend:
    return LineFrontend()


factory.display_name = "Plain lines"  # type: ignore[attr-defined]
factory.kind = "terminal"  # type: ignore[attr-defined]  # started by `kayden`
