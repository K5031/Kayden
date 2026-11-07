"""Instance and runtime configuration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kayden.api import Budget, PermissionPolicy

DEFAULT_SYSTEM_PROMPT = (
    "You are Kayden, an AI agent. Use the available tools to accomplish the user's goals.\n"
    "Tool calls that take long are moved to the background: you get an operation id, can "
    "check them with op_status or cancel them with op_cancel, and their outcome arrives later "
    "as an operation result. Use recall to search earlier parts of this conversation that are "
    "no longer in context, and peek_other_mode to read (never change) the memory of the other "
    "operating mode."
)

ACTIVATION_NOTICE = (
    "[Runtime notice] You were activated without a new user message. Continue your work, "
    "or idle if there is nothing to do."
)

CONTINUOUS_PROMPT = (
    "You run continuously. When you have nothing more to do right now, call idle(); ending a "
    "turn without tool calls also idles. Background operations keep running while you are "
    "idle. Calling idle() also conserves your budget."
)


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    """A module as an import path "package.module:factory" plus its configuration."""

    factory: str
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Provider:
    """A model provider the user can switch to: a Core and the key it reads."""

    id: str
    name: str
    core: ModuleSpec
    key_name: str | None = None  # environment variable and keychain entry of its key


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Instance-level settings. The user settings among them change through
    RuntimeAPI.update_settings; the rest are fixed for the process."""

    data_dir: Path
    budget: Budget = field(default_factory=Budget)
    permissions: PermissionPolicy = PermissionPolicy.ASK
    model: str | None = None  # default model; None = first model Core lists
    maintenance_model: str | None = None  # Completion(light=True); None = main model
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    context_windows: Mapping[str, int] = field(default_factory=dict)  # for unknown windows
    default_max_output_tokens: int = 4096
    max_retries: int = 3
    retry_base_delay_s: float = 1.0
    retry_max_delay_s: float = 30.0
    initial_wait_s: float = 10.0
    default_tool_timeout_s: float = 3600.0
    cancel_grace_s: float = 5.0
    subscriber_queue_size: int = 1000
    providers: Sequence[Provider] = ()  # what the user can switch to
    provider: str | None = None  # Provider.id of the Core the instance was built with
    tools_module: ModuleSpec | None = None  # rebuilt with options["root"] = work folder


@dataclass(frozen=True, slots=True)
class InstanceConfig:
    runtime: RuntimeConfig
    core: ModuleSpec
    memory: ModuleSpec
    tools: ModuleSpec
    frontend: ModuleSpec
