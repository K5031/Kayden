"""Instance and runtime configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kayden.api import Budget, PermissionPolicy

DEFAULT_SYSTEM_PROMPT = (
    "You are Kayden, an AI agent. Use the available tools to accomplish the user's goals.\n"
    "Tool calls that take long are moved to the background: you get an operation id, can "
    "check them with op_status or cancel them with op_cancel, and their outcome arrives later "
    "as an operation result. Use recall to search earlier parts of this conversation that are "
    "no longer in context, or with scope \"all\" the user's other conversations."
)

ACTIVATION_NOTICE = (
    "[Runtime notice] You were activated without a new user message. Continue your work, "
    "or idle if there is nothing to do."
)

# A reply must not be followed by a tool call: some models return text that precedes a
# call as hidden reasoning, so a model that reports and then calls idle() says nothing.
AUTONOMY_PROMPT = (
    "This conversation is autonomous: the user hands you a task and may not be watching. "
    "Work on it until it is done or you cannot go on without the user. Make reasonable "
    "choices yourself instead of asking, and say in your reply which ones you made; ask "
    "only when a wrong guess would be costly or could not be undone. Actions still need "
    "the permission the user's settings require; when one is refused, go on with what you "
    "can do. Operations you started in the background keep running after your reply, and "
    "when they finish you are woken to continue without waiting for the user. When you are "
    "done for now, end your turn with a reply to the user, as in any conversation, and call "
    "no tool after it: the user may not see text that is followed by a tool call. Call "
    "idle only to wait for some of the running operations instead of all of them, with at "
    "most one short sentence before it."
)


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    """A module as an import path "package.module:factory" plus its configuration."""

    factory: str
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModuleState:
    """The module settings of an instance, by kind (api.MODULE_KINDS). An id is an entry
    point name; None is a module given by import path in config.toml."""

    running: Mapping[str, str | None] = field(default_factory=dict)  # built for this run
    chosen: Mapping[str, str | None] = field(default_factory=dict)  # saved for the next start
    work_folder: str | None = None  # the Tools root this run; None = not settable


@dataclass(frozen=True, slots=True)
class Restart:
    """What RuntimeAPI.restart() asked the launcher for."""

    frontend: str | None  # id of the frontend to bring up; None = the configured one
    frontend_kind: str  # FrontendInfo.kind of that frontend
    conversation_id: str | None  # the conversation to reopen; None = let the frontend ask


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
    default_max_output_tokens: int = 4096
    max_retries: int = 3
    retry_base_delay_s: float = 1.0
    retry_max_delay_s: float = 30.0
    initial_wait_s: float = 10.0
    default_tool_timeout_s: float = 3600.0
    cancel_grace_s: float = 5.0
    subscriber_queue_size: int = 1000
    modules: ModuleState = field(default_factory=ModuleState)


@dataclass(frozen=True, slots=True)
class InstanceConfig:
    runtime: RuntimeConfig
    core: ModuleSpec
    memory: ModuleSpec
    tools: ModuleSpec
    frontend: ModuleSpec
    # Why the modules cannot be built (a saved module that is not installed); the frontend
    # then opens on a setup runtime that shows it.
    startup_error: str | None = None
