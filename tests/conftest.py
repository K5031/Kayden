from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import keyring
import keyring.backend
import pytest

from kayden.api import (
    PERMISSION_CEILING_ENV,
    Activation,
    AgentState,
    ConversationInfo,
    Decision,
    Message,
    Mode,
    PermissionPolicy,
    PermissionRequested,
    Role,
    StartOptions,
    ToolResult,
)
from kayden.runtime import Runtime, RuntimeConfig
from tests.fakes.core import ScriptedCore
from tests.fakes.frontend import Recorder, ScriptedFrontend
from tests.fakes.memory import InMemoryMemory, MemoryStore
from tests.fakes.tools import FakeTools

FAST: dict[str, Any] = {
    "initial_wait_s": 0.15,
    "cancel_grace_s": 0.2,
    "retry_base_delay_s": 0.01,
    "retry_max_delay_s": 0.05,
    "permissions": PermissionPolicy.ALLOW_READ_ONLY,
}


async def eventually(predicate: Callable[[], Any], timeout: float = 5.0) -> Any:
    async def poll() -> Any:
        while not (result := predicate()):
            await asyncio.sleep(0.005)
        return result

    return await asyncio.wait_for(poll(), timeout)


@dataclass
class Harness:
    runtime: Runtime
    core: ScriptedCore
    tools: FakeTools
    memory: InMemoryMemory
    store: MemoryStore
    rec: Recorder
    data_dir: Path
    trace: list[Any] = field(default_factory=list)
    conversation: ConversationInfo | None = None
    closed: bool = False

    async def start(
        self,
        mode: Mode = Mode.SESSION,
        activation: Activation = Activation.USER_CONTROLLED,
        conversation_id: str | None = None,
        model: str | None = None,
    ) -> ConversationInfo:
        self.conversation = await self.runtime.start(
            StartOptions(mode, activation, conversation_id, model)
        )
        return self.conversation

    async def transcript(self) -> list[Message]:
        assert self.conversation is not None
        return list(await self.runtime.history(self.conversation.id))

    async def results(self) -> dict[str, ToolResult]:
        return {
            b.call_id: b
            for m in await self.transcript()
            if m.role == Role.TOOL
            for b in m.blocks
            if isinstance(b, ToolResult)
        }

    async def turn(self, text: str) -> None:
        """Send and wait until the agent is IDLE again."""
        seq = self.runtime.snapshot().seq
        await self.runtime.send(text)
        await self.rec.wait_for(lambda r: any(
            env.seq > seq and getattr(env.event, "state", None) == AgentState.IDLE
            for env in r.envelopes))

    def auto_approve(self, decision: Decision = Decision.ALLOW_ONCE) -> asyncio.Task[None]:
        async def approve() -> None:
            seen: set[str] = set()
            while True:
                for e in self.rec.of(PermissionRequested):
                    if e.request.id not in seen:
                        seen.add(e.request.id)
                        await self.runtime.resolve_permission(e.request.id, decision)
                await asyncio.sleep(0.005)

        return asyncio.create_task(approve())

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.tools.release.set()
        await self.runtime.aclose()
        await self.rec.close()


type Make = Callable[..., Any]


class MemoryKeyring(keyring.backend.KeyringBackend):
    """Stands in for the OS keychain so tests never read or write the real one."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def keychain() -> Any:
    previous = keyring.get_keyring()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    yield memory
    keyring.set_keyring(previous)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    # Record the variable so whatever the runtime exports is undone after the test.
    monkeypatch.setenv(PERMISSION_CEILING_ENV, "allow_read_only")
    monkeypatch.delenv(PERMISSION_CEILING_ENV)
    return monkeypatch


@pytest.fixture
async def make(tmp_path: Path, clean_env: pytest.MonkeyPatch):
    harnesses: list[Harness] = []

    async def _make(
        *,
        core: ScriptedCore | None = None,
        tools: FakeTools | None = None,
        store: MemoryStore | None = None,
        memory_kw: dict[str, Any] | None = None,
        data_dir: Path | None = None,
        interactive: bool = True,
        **config: Any,
    ) -> Harness:
        trace: list[Any] = []
        core = core or ScriptedCore()
        core.trace = trace
        tools = tools or FakeTools()
        tools.trace = trace
        store = store if store is not None else MemoryStore()
        memory = InMemoryMemory(store, trace=trace, **(memory_kw or {}))
        data_dir = data_dir or tmp_path / f"instance{len(harnesses)}"
        frontend = ScriptedFrontend(interactive=interactive)
        runtime = Runtime(
            core=core,
            memory=memory,
            tools=tools,
            frontend=frontend.info,
            config=RuntimeConfig(data_dir=data_dir, **{**FAST, **config}),
        )
        memory.seq_source = lambda: runtime.snapshot().seq
        h = Harness(runtime, core, tools, memory, store, frontend.attach(runtime), data_dir, trace)
        harnesses.append(h)
        return h

    yield _make
    for h in harnesses:
        await h.close()
