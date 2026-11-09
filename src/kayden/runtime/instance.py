"""Instance safety (data-directory lock), module loading and the process entry point."""

from __future__ import annotations

import asyncio
import fcntl
import importlib
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from kayden.api import Core, Frontend, InstancePaths, Memory, Tools
from kayden.runtime.config import InstanceConfig, ModuleSpec, Restart
from kayden.runtime.modules import targets

LOCK_FILE = "instance.lock"
# Locks of the Kayden versions that ran one store per mode. While an older Kayden holds
# one, this data directory is in use.
LEGACY_LOCK_FILES = ("session.lock", "continuous.lock")


class InstanceLockedError(RuntimeError):
    """Another process uses this data directory."""


class ModuleLoadError(Exception):
    """A module could not be built from its configuration (bad factory path, bad options,
    missing credentials). The message names the factory and fits on one line."""


class InstanceLock:
    """OS-level advisory lock per data directory; released on close or process exit. Also
    holds any per-mode lock file an older Kayden left, so that version cannot start on the
    directory meanwhile."""

    def __init__(self, path: Path, fds: list[int]):
        self.path = path
        self._fds = fds

    @classmethod
    def acquire(cls, data_dir: Path) -> InstanceLock:
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / LOCK_FILE
        legacy = [data_dir / name for name in LEGACY_LOCK_FILES if (data_dir / name).exists()]
        fds: list[int] = []
        try:
            for lock_path in (path, *legacy):
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    holder = lock_path.read_text().strip() or "unknown"
                    raise InstanceLockedError(
                        f"{data_dir} is already in use by process {holder}") from None
                fds.append(fd)
        except BaseException:
            for fd in fds:
                os.close(fd)
            raise
        os.ftruncate(fds[0], 0)
        os.write(fds[0], f"{os.getpid()}\n".encode())
        return cls(path, fds)

    def release(self) -> None:
        for fd in self._fds:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self._fds = []


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
    if error is None:
        try:
            # Before any module: no Memory access without the data directory.
            lock = InstanceLock.acquire(runtime_config.data_dir)
            for spec, kind in ((config.core, Core), (config.memory, Memory),
                               (config.tools, Tools)):
                built.append(await asyncio.to_thread(load_module, spec, kind, paths))
        except (InstanceLockedError, ModuleLoadError) as e:
            error = (f"Kayden is already open in another window ({e})"
                     if isinstance(e, InstanceLockedError) else str(e))
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
        runtime = SetupRuntime(runtime_config, frontend.info.kind, error)
    try:
        await frontend.run(runtime)
    finally:
        await runtime.aclose()
        await frontend.aclose()
    return runtime.restart_request
