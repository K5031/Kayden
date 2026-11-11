from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import keyring
import keyring.backend
import pytest

import kayden.frontends.desktop  # noqa: F401 - before any Qt import: see its lazy-import note
from kayden.api import (
    PERMISSION_CEILING_ENV,
    AgentState,
    ConversationInfo,
    Decision,
    Message,
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


if os.environ.get("KAYDEN_TEST_LOOP") == "qasync":
    def pytest_asyncio_loop_factories(config: pytest.Config, item: pytest.Item) -> Any:
        """KAYDEN_TEST_LOOP=qasync runs every async test on qasync's QEventLoop, the loop
        the desktop app runs on."""
        import qasync
        from PySide6.QtWidgets import QApplication

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        return {"qasync": lambda: qasync.QEventLoop(app)}


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
        conversation_id: str | None = None,
        model: str | None = None,
        **options: Any,
    ) -> ConversationInfo:
        self.conversation = await self.runtime.start(
            StartOptions(conversation_id, model, **options)
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


# Entry points of a package that provides modules, the way a user-made one would.
INSTALLED_MODULES = """\
[kayden.cores]
first = tests.fakes.core:first_factory
second = tests.fakes.core:second_factory
keyless = tests.fakes.core:factory
broken = tests.fakes.nowhere:factory

[kayden.memories]
fake-memory = tests.fakes.memory:factory

[kayden.tools]
fake-tools = tests.fakes.tools:factory

[kayden.frontends]
scripted = tests.fakes.frontend:factory
scripted-window = tests.fakes.frontend:window_factory
scripted-window-2 = tests.fakes.frontend:other_window_factory
"""


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Installs a package declaring INSTALLED_MODULES: its dist-info on sys.path."""
    site = tmp_path / "site"
    dist = site / "kayden_test_modules-1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: kayden-test-modules\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text(INSTALLED_MODULES)
    monkeypatch.syspath_prepend(str(site))
    return site


@pytest.fixture
def child_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Child Kayden processes (sub-sessions) import the test fakes their config names."""
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))


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
        more_tools: Sequence[FakeTools] = (),
        store: MemoryStore | None = None,
        memory_kw: dict[str, Any] | None = None,
        data_dir: Path | None = None,
        interactive: bool = True,
        kind: str = "terminal",
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
        frontend = ScriptedFrontend(interactive=interactive, kind=kind)
        runtime = Runtime(
            core=core,
            memory=memory,
            tools=[tools, *more_tools],
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
