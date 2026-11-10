"""Instance safety (data-directory lock), module loading and the process entry point."""

from __future__ import annotations

import asyncio
import fcntl
import importlib
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from kayden.api import (
    Core,
    Frontend,
    InstanceLockedError,
    InstancePaths,
    Memory,
    Tools,
)
from kayden.runtime.config import InstanceConfig, ModuleSpec, Restart
from kayden.runtime.modules import targets

LOCK_FILE = "instance.lock"
class ModuleLoadError(Exception):
    """A module could not be built from its configuration (bad factory path, bad options,
    missing credentials). The message names the factory and fits on one line."""


class InstanceLock:
    """OS-level advisory lock per data directory; released on close or process exit."""

    def __init__(self, path: Path, fd: int):
        self.path = path
        self._fd: int | None = fd

    @classmethod
    def acquire(cls, data_dir: Path) -> InstanceLock:
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / LOCK_FILE
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            holder = path.read_text().strip() or "unknown"
            raise InstanceLockedError(
                f"Kayden is already open in another window ({data_dir} is in use by "
                f"process {holder})") from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        return cls(path, fd)

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def load_module[T](spec: ModuleSpec, kind: type[T], paths: InstancePaths) -> T:
    """Import "package.module:factory" and call factory(options, paths)."""
    module_name, sep, attr = spec.factory.partition(":")
    if not sep or not attr:
        raise ModuleLoadError(
            f"module path must be 'package.module:factory', got {spec.factory!r}")
    try:
        factory: Any = getattr(importlib.import_module(module_name), attr)
        module = factory(dict(spec.options), paths)
    except (ImportError, AttributeError, ValueError, TypeError) as e:
        raise ModuleLoadError(f"{spec.factory}: {e}") from e
    if not isinstance(module, kind):
        raise ModuleLoadError(
            f"{spec.factory} returned {type(module).__name__}, not {kind.__name__}")
    return module


async def run_instance(config: InstanceConfig, *, fallback: str | None = None
                       ) -> Restart | None:
    """Build the instance from its configuration, run the frontend, then shut down; the
    Restart the frontend asked for, if any. The data directory lock is taken before Core,
    Memory and Tools are built. When it is refused, they cannot be built, or
    config.startup_error is set, the frontend runs on a SetupRuntime that shows why. A
    frontend that cannot be built is replaced by the `fallback` frontend id."""
    from kayden.runtime.orchestrator import Runtime
    from kayden.runtime.setup import SetupRuntime

    paths = InstancePaths(config.runtime.data_dir)
    error = config.startup_error
    runtime_config = config.runtime
    # Off the event loop: factories may block briefly (a Core reading the OS keychain).
    try:
        frontend = await asyncio.to_thread(load_module, config.frontend, Frontend, paths)
    except ModuleLoadError as e:
        target = targets("frontend").get(fallback or "")
        if target is None:
            raise
        frontend = await asyncio.to_thread(load_module, ModuleSpec(target), Frontend, paths)
        error = error or str(e)
        state = runtime_config.modules
        runtime_config = replace(runtime_config, modules=replace(
            state, running={**state.running, "frontend": fallback}))
    built: list[Any] = []
    lock: InstanceLock | None = None
    already_open = False
    if error is None:
        try:
            # Before any module: no Memory access without the data directory.
            lock = InstanceLock.acquire(runtime_config.data_dir)
            for spec, kind in ((config.core, Core), (config.memory, Memory),
                               (config.tools, Tools)):
                built.append(await asyncio.to_thread(load_module, spec, kind, paths))
        except (InstanceLockedError, ModuleLoadError) as e:
            error, already_open = str(e), isinstance(e, InstanceLockedError)
            for module in built:
                await module.aclose()
            if lock is not None:
                lock.release()
    runtime: Runtime | SetupRuntime
    if error is None:
        core, memory, tools = built
        runtime = Runtime(core=core, memory=memory, tools=tools, frontend=frontend.info,
                          config=runtime_config, lock=lock)
    else:
        runtime = SetupRuntime(runtime_config, frontend.info.kind, error,
                               already_open=already_open)
    try:
        await frontend.run(runtime)
    finally:
        await runtime.aclose()
        await frontend.aclose()
    return runtime.restart_request
