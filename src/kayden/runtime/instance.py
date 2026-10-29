"""Instance safety (data-directory lock), module loading and the process entry point."""

from __future__ import annotations

import fcntl
import importlib
import os
from pathlib import Path
from typing import Any

from kayden.api import Core, Frontend, InstancePaths, Memory, Mode, Tools
from kayden.runtime.config import InstanceConfig, ModuleSpec


class InstanceLockedError(RuntimeError):
    """Another process holds this (data directory, mode)."""


class InstanceLock:
    """OS-level advisory lock per (data directory, mode); released on close or process exit."""

    def __init__(self, path: Path, fd: int):
        self.path = path
        self._fd: int | None = fd

    @classmethod
    def acquire(cls, data_dir: Path, mode: Mode) -> InstanceLock:
        path = data_dir / f"{mode}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = path.read_text().strip() or "unknown"
            os.close(fd)
            raise InstanceLockedError(
                f"{mode} mode in {data_dir} is already in use by process {holder}"
            ) from None
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
        raise ValueError(f"module path must be 'package.module:factory', got {spec.factory!r}")
    factory: Any = getattr(importlib.import_module(module_name), attr)
    module = factory(dict(spec.options), paths)
    if not isinstance(module, kind):
        raise TypeError(f"{spec.factory} returned {type(module).__name__}, not {kind.__name__}")
    return module


async def run_instance(config: InstanceConfig) -> None:
    """Build the instance from its configuration, run the frontend, then shut down."""
    from kayden.runtime.orchestrator import Runtime

    paths = InstancePaths(config.runtime.data_dir)
    core = load_module(config.core, Core, paths)
    memory = load_module(config.memory, Memory, paths)
    tools = load_module(config.tools, Tools, paths)
    frontend = load_module(config.frontend, Frontend, paths)
    runtime = Runtime(core=core, memory=memory, tools=tools, frontend=frontend.info,
                      config=config.runtime)
    try:
        await frontend.run(runtime)
    finally:
        await runtime.aclose()
        await frontend.aclose()
