"""Instance safety (data-directory lock), module loading and the process entry point."""

from __future__ import annotations

import asyncio
import fcntl
import importlib
import os
from pathlib import Path
from typing import Any

from kayden.api import Core, Frontend, InstancePaths, Memory, Tools
from kayden.runtime.config import InstanceConfig, ModuleSpec

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
        raise ValueError(f"module path must be 'package.module:factory', got {spec.factory!r}")
    try:
        factory: Any = getattr(importlib.import_module(module_name), attr)
        module = factory(dict(spec.options), paths)
    except (ImportError, AttributeError, ValueError, TypeError) as e:
        raise ModuleLoadError(f"{spec.factory}: {e}") from e
    if not isinstance(module, kind):
        raise TypeError(f"{spec.factory} returned {type(module).__name__}, not {kind.__name__}")
    return module


async def run_instance(config: InstanceConfig) -> None:
    """Build the instance from its configuration, run the frontend, then shut down."""
    from kayden.runtime.orchestrator import Runtime

    paths = InstancePaths(config.runtime.data_dir)
    # Off the event loop: factories may block briefly (a Core reading the OS keychain).
    core = await asyncio.to_thread(load_module, config.core, Core, paths)
    memory = await asyncio.to_thread(load_module, config.memory, Memory, paths)
    tools = await asyncio.to_thread(load_module, config.tools, Tools, paths)
    frontend = await asyncio.to_thread(load_module, config.frontend, Frontend, paths)
    runtime = Runtime(core=core, memory=memory, tools=tools, frontend=frontend.info,
                      config=config.runtime)
    try:
        await frontend.run(runtime)
    finally:
        await runtime.aclose()
        await frontend.aclose()
