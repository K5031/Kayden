"""One running tool call: timeouts, stall detection, cancellation and abandonment."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Awaitable, Callable

from kayden.api import ToolCall, ToolOutput, ToolResult, ToolSpec, ToolStatus


class Context:
    """ToolContext handed to Tools.execute. progress() is safe from any thread."""

    def __init__(self, op: Operation, conversation_id: str):
        self.call_id = op.call.id
        self.conversation_id = conversation_id
        self._op = op
        self._loop = asyncio.get_running_loop()
        self._thread = threading.get_ident()

    def progress(self, text: str) -> None:
        if threading.get_ident() == self._thread:
            self._op.report_progress(text)
        else:
            self._loop.call_soon_threadsafe(self._op.report_progress, text)


class Operation:
    def __init__(
        self,
        call: ToolCall,
        spec: ToolSpec,
        *,
        default_timeout: float,
        grace: float,
        on_progress: Callable[[Operation, str], None],
        on_late: Callable[[Operation, ToolResult], None],
    ):
        self.call = call
        self.spec = spec
        self.started_at = time.time()
        self.started_mono = time.monotonic()
        self.last_activity = self.started_mono
        self.last_progress: str | None = None
        self.backgrounded = False
        self.abandoned = False
        self.result: ToolResult | None = None
        self.supervisor: asyncio.Task[ToolResult] | None = None
        timeout = spec.timeout if spec.timeout is not None else default_timeout
        self._deadline = None if math.isinf(timeout) else self.started_mono + timeout
        self._grace = grace
        self._on_progress = on_progress
        self._on_late = on_late
        self._cancel_status: ToolStatus | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[ToolOutput] | None = None

    @property
    def duration(self) -> float:
        return time.monotonic() - self.started_mono

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_status is not None

    def report_progress(self, text: str) -> None:
        if self.result is not None:
            return
        self.last_activity = time.monotonic()
        if text:
            self.last_progress = text
            self._on_progress(self, text)

    def request_cancel(self) -> None:
        if self.result is None and self._cancel_status is None:
            self._cancel_status = ToolStatus.CANCELLED
            self._wake.set()

    def _next_check(self) -> float | None:
        now = time.monotonic()
        waits = []
        if self._deadline is not None:
            waits.append(self._deadline - now)
        if self.spec.stall_timeout is not None:
            waits.append(self.last_activity + self.spec.stall_timeout - now)
        return max(0.0, min(waits)) if waits else None

    def _timed_out(self) -> bool:
        now = time.monotonic()
        if self._deadline is not None and now >= self._deadline:
            return True
        stall = self.spec.stall_timeout
        return stall is not None and now - self.last_activity >= stall

    def _progress_note(self) -> str:
        return f" Last progress: {self.last_progress}" if self.last_progress else ""

    async def run(self, execute: Callable[[], Awaitable[ToolOutput]]) -> ToolResult:
        self._task = asyncio.ensure_future(execute())
        status: ToolStatus | None = None
        while not self._task.done():
            if self._cancel_status is not None:
                status = self._cancel_status
                break
            if self._timed_out():
                status = ToolStatus.TIMED_OUT
                break
            self._wake.clear()
            waker = asyncio.ensure_future(self._wake.wait())
            try:
                await asyncio.wait(
                    {self._task, waker},
                    timeout=self._next_check(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                waker.cancel()
        if not self._task.done():
            self._task.cancel()
            await asyncio.wait({self._task}, timeout=self._grace)
            if not self._task.done():
                self.abandoned = True
                self._task.add_done_callback(self._late_completion)
                cause = "timed out" if status == ToolStatus.TIMED_OUT else "was cancelled"
                return ToolResult(
                    self.call.id,
                    ToolStatus.ABANDONED,
                    f"The call {cause} but did not confirm cancellation within "
                    f"{self._grace:g}s and was abandoned; it may still have effects."
                    + self._progress_note(),
                )
        return self._outcome(self._task, status)

    def _outcome(self, task: asyncio.Task[ToolOutput], status: ToolStatus | None) -> ToolResult:
        cid = self.call.id
        if task.cancelled():
            status = status or ToolStatus.CANCELLED
            label = "Timed out" if status == ToolStatus.TIMED_OUT else "Cancelled"
            return ToolResult(cid, status, label + "." + self._progress_note())
        exc = task.exception()
        if exc is not None:
            return ToolResult(cid, ToolStatus.ERROR, f"Tool raised {type(exc).__name__}: {exc}")
        output = task.result()
        if not isinstance(output, ToolOutput):
            return ToolResult(cid, ToolStatus.ERROR, f"Tool returned {type(output).__name__}")
        return ToolResult(cid, ToolStatus.ERROR if output.is_error else ToolStatus.OK,
                          output.content, tuple(output.attachments))

    def _late_completion(self, task: asyncio.Task[ToolOutput]) -> None:
        self._on_late(self, self._outcome(task, None))
