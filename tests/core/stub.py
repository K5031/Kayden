"""In-process HTTP stub for provider SDKs, mounted via httpx2.MockTransport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import httpx2


class SSEStream(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes], hang: bool) -> None:
        self.chunks = chunks
        self.hang = hang
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.hang:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class Reply:
    status: int = 200
    events: list[tuple[str | None, dict | str]] = field(default_factory=list)  # (event, data)
    json_body: dict | None = None
    headers: dict[str, str] = field(default_factory=dict)
    hang: bool = False
    stream: SSEStream | None = None
    raw: bytes | None = None  # sent verbatim instead of `events`


class StubProvider:
    """Queued replies for the streaming endpoint; `routes` answer everything else."""

    def __init__(self, stream_path: str) -> None:
        self.stream_path = stream_path
        self.replies: list[Reply] = []
        self.bodies: list[dict] = []
        self.routes: dict[str, Callable[[httpx2.Request], httpx2.Response]] = {}

    def queue(self, reply: Reply) -> Reply:
        self.replies.append(reply)
        return reply

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path != self.stream_path:
            return self.routes[path](request)
        self.bodies.append(json.loads(request.content))
        reply = self.replies.pop(0)
        if reply.json_body is not None or reply.status != 200:
            return httpx2.Response(reply.status, headers=reply.headers, json=reply.json_body or {})
        chunks = []
        for event, data in reply.events:
            payload = data if isinstance(data, str) else json.dumps(data)
            prefix = f"event: {event}\n" if event else ""
            chunks.append(f"{prefix}data: {payload}\n\n".encode())
        if reply.raw is not None:
            chunks = [reply.raw]
        reply.stream = SSEStream(chunks, reply.hang)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=reply.stream
        )
