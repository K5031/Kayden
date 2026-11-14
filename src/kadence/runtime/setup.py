"""The setup runtime: what a frontend runs on when the modules could not be built."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict, fields, replace
from typing import NoReturn

from kadence.api import (
    AgentState,
    ArchiveHit,
    CallId,
    ConversationId,
    ConversationInfo,
    Decision,
    EventEnvelope,
    MemoryNote,
    MemoryView,
    Message,
    ModelInfo,
    PermissionPolicy,
    Refusal,
    RuntimeSnapshot,
    RuntimeStateError,
    Settings,
    SettingsChanged,
    SettingsUpdate,
    ShuttingDown,
    Skill,
    StartOptions,
    ToolSpec,
    Usage,
    UsageSnapshot,
)
from kadence.runtime.config import Restart, RuntimeConfig
from kadence.runtime.events import EventBus, EventLog
from kadence.runtime.modules import ModuleSettings


class SetupRuntime:
    """Implements RuntimeAPI without Core, Memory and Tools. settings() carries the startup
    error; module settings and the runtime settings that need no Core (budget, permissions,
    system prompt) are saved for the next start; restart() tries again. Every other command
    raises RuntimeStateError. Nothing is locked, so the event log is not written."""

    def __init__(self, config: RuntimeConfig, frontend_kind: str, error: str, *,
                 already_open: bool = False):
        self._config = config
        self._error = error
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self._ceiling = PermissionPolicy.inherited_ceiling()
        self._closed = False
        self._modules = ModuleSettings(config.data_dir, config.modules, frontend_kind, error,
                                       already_open)
        self._bus = EventBus(config.subscriber_queue_size, EventLog())
        self.restart_request: Restart | None = None

    def _refuse(self) -> NoReturn:
        raise RuntimeStateError(f"Kadence could not start: {self._error}",
                                Refusal.UNAVAILABLE)

    # Observation

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._bus.subscribe()

    def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(self._bus.seq, AgentState.NOT_STARTED, None, None,
                               self._config.model, UsageSnapshot(Usage(), 0, 0.0), [], [], [])

    async def settings(self) -> Settings:
        cfg = self._config
        permissions = cfg.permissions
        if self._ceiling is not None:
            permissions = permissions.clamp(self._ceiling)
        return Settings(model=cfg.model, maintenance_model=cfg.maintenance_model,
                        budget=cfg.budget, permissions=permissions,
                        system_prompt=cfg.system_prompt, **await self._modules.fields())

    async def update_settings(self, update: SettingsUpdate) -> Settings:
        if update.model is not None or update.maintenance_model is not None:
            raise ValueError("the model can be chosen once Kadence has started")
        if update.system_prompt is not None and not update.system_prompt.strip():
            raise ValueError("the system prompt is empty")
        saved = await self._modules.prepare(update)
        if update.budget is not None:
            saved["budget"] = {k: v for k, v in asdict(update.budget).items() if v is not None}
        if update.permissions is not None:
            saved["permissions"] = update.permissions.value
        if update.system_prompt is not None:
            saved["system_prompt"] = update.system_prompt
        await self._modules.commit(update, saved)
        self._config = replace(self._config, budget=update.budget or self._config.budget,
                               permissions=update.permissions or self._config.permissions,
                               system_prompt=update.system_prompt or self._config.system_prompt)
        settings = await self.settings()
        if any(getattr(update, f.name) is not None for f in fields(update)):
            self._bus.emit(SettingsChanged(settings))
        return settings

    async def restart(self) -> None:
        if self.restart_request is None:
            self.restart_request = await self._modules.restart(None)
            await self.aclose()

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus.emit(ShuttingDown("shutdown"))

    # Everything else needs the modules.

    async def list_models(self) -> Sequence[ModelInfo]:
        self._refuse()

    async def list_tools(self) -> Sequence[ToolSpec]:
        self._refuse()

    async def list_conversations(self, *, include_archived: bool = False
                                 ) -> Sequence[ConversationInfo]:
        self._refuse()

    async def history(self, conversation_id: ConversationId, *, before: str | None = None,
                      limit: int | None = None) -> Sequence[Message]:
        self._refuse()

    async def search_archive(self, query: str, *, conversation_id: ConversationId | None = None,
                             limit: int = 10) -> Sequence[ArchiveHit]:
        self._refuse()

    async def inspect_memory(self, conversation_id: ConversationId) -> MemoryView | None:
        self._refuse()

    async def notes(self) -> Sequence[MemoryNote]:
        self._refuse()

    async def skills(self) -> Sequence[Skill]:
        return ()

    async def start(self, options: StartOptions) -> ConversationInfo:
        self._refuse()

    async def select_model(self, model_id: str) -> None:
        self._refuse()

    async def send(self, text: str) -> None:
        self._refuse()

    async def notify(self, text: str) -> None:
        self._refuse()

    async def activate(self) -> None:
        self._refuse()

    async def stop(self) -> None:
        return None

    async def cancel_operation(self, call_id: CallId) -> None:
        self._refuse()

    async def rename_conversation(self, conversation_id: ConversationId, title: str) -> None:
        self._refuse()

    async def archive_conversation(self, conversation_id: ConversationId,
                                   archived: bool) -> None:
        self._refuse()

    async def delete_conversation(self, conversation_id: ConversationId) -> None:
        self._refuse()

    async def save_note(self, title: str, description: str, text: str, *,
                        note_id: str | None = None) -> MemoryNote:
        self._refuse()

    async def delete_note(self, note_id: str) -> None:
        self._refuse()

    async def resolve_permission(self, request_id: str, decision: Decision) -> None:
        self._refuse()
