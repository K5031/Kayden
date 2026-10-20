"""Qt frontend module entrypoint."""

import json
import queue
import threading
from typing import Callable

from kayden.frontend_api import (
    ApplyModuleSelections,
    AssistantText,
    CancelCapture,
    CaptureConfigured,
    CaptureStateChanged,
    ControlsConfigured,
    DraftChanged,
    FinishCapture,
    IdentityConfigured,
    InterruptTurn,
    InvokeControl,
    ModelsConfigured,
    ModulesConfigured,
    PermissionRequested,
    ResolvePermission,
    SelectModel,
    StartCapture,
    StatusChanged,
    SubmitMessage,
    ToolActivityFinished,
    ToolActivityStarted,
    TurnFinished,
    UserMessage,
)
from kayden.interfaces import FrontendInterface


class Module(FrontendInterface):
    """Native Qt Quick frontend backed by PySide6 and QML."""

    frontend_api_version = 1

    def __init__(self, width=1000, height=760):
        if not isinstance(width, int) or width < 480:
            raise ValueError("qt frontend width must be an integer of at least 480")
        if not isinstance(height, int) or height < 420:
            raise ValueError("qt frontend height must be an integer of at least 420")
        self.width = width
        self.height = height
        self.assistant_name = "Kayden"
        self._events = queue.SimpleQueue()
        self._models = []
        self._selected_model = None
        self._on_model_select = None
        self._module_catalog = []
        self._on_modules_apply = None
        self._module_change_busy = False
        self._module_change_thread = None
        self._controls = []
        self._capture_label = "Capture"
        self._on_capture_start = None
        self._on_capture_finish = None
        self._on_capture_cancel = None
        self._on_submit = None
        self._on_interrupt = None
        self._closed = threading.Event()
        self._tool_activities = {}
        self._tool_chain_open = False
        self._tool_group_id = 0
        self._api = None
        self._unsubscribe = None

    def _accept_event(self, event) -> None:
        if isinstance(event, IdentityConfigured):
            self.configure_identity(event.assistant_name)
        elif isinstance(event, ModelsConfigured):
            self.configure_models(
                event.models,
                event.selected_model,
                lambda model: self._api.send(SelectModel(model)),
            )
        elif isinstance(event, ModulesConfigured):
            self.configure_modules(
                event.modules,
                lambda selections: self._api.send(
                    ApplyModuleSelections(selections)
                ),
            )
        elif isinstance(event, CaptureConfigured):
            if event.available:
                self.configure_capture(
                    lambda: self._api.send(StartCapture()),
                    lambda: self._api.send(FinishCapture()),
                    lambda: self._api.send(CancelCapture()),
                    event.label,
                )
            else:
                self.clear_capture()
        elif isinstance(event, ControlsConfigured):
            self.configure_controls(event.controls)
        elif isinstance(event, UserMessage):
            self.user(event.text)
        elif isinstance(event, AssistantText):
            self.assistant(event.text)
        elif isinstance(event, StatusChanged):
            self.status(event.text)
        elif isinstance(event, ToolActivityStarted):
            self.tool_started(
                event.call_id,
                event.name,
                event.arguments,
                event.description,
            )
        elif isinstance(event, ToolActivityFinished):
            self.tool_finished(
                event.call_id,
                event.name,
                event.result,
                event.description,
            )
        elif isinstance(event, TurnFinished):
            self.finish(event.interrupted)
        elif isinstance(event, CaptureStateChanged):
            self.capture_state(event.state)
        elif isinstance(event, DraftChanged):
            self.draft(event.text)
        elif isinstance(event, PermissionRequested):
            self.permission_requested(event)

    def configure_identity(self, assistant_name: str) -> None:
        self.assistant_name = assistant_name

    def configure_models(self, models, selected_model, on_select) -> bool:
        normalized = []
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            name = model.get("name")
            if not isinstance(model_id, str) or not model_id:
                continue
            normalized.append({
                "id": model_id,
                "name": name if isinstance(name, str) and name else model_id,
            })
        if not normalized or selected_model not in {
            model["id"] for model in normalized
        }:
            return False
        self._models = normalized
        self._selected_model = selected_model
        self._on_model_select = on_select
        return True

    def configure_modules(self, modules, on_apply) -> bool:
        normalized = []
        for category in modules:
            if not isinstance(category, dict):
                continue
            capability = category.get("id")
            options = category.get("options")
            selected = category.get("selected", [])
            if not isinstance(capability, str) or not isinstance(options, list):
                continue
            valid_options = [
                {"id": option["id"], "name": option.get("name") or option["id"]}
                for option in options
                if isinstance(option, dict)
                and isinstance(option.get("id"), str)
                and option["id"]
            ]
            valid_ids = {option["id"] for option in valid_options}
            selected = [
                name for name in selected
                if isinstance(name, str) and name in valid_ids
            ]
            normalized.append({
                "id": capability,
                "name": category.get("name") or capability,
                "multiple": bool(category.get("multiple")),
                "optional": bool(category.get("optional")),
                "options": valid_options,
                "selected": selected,
            })
        if not normalized:
            return False
        self._module_catalog = normalized
        self._on_modules_apply = on_apply
        self._events.put(("modules", list(normalized)))
        return True

    def configure_capture(
        self,
        on_start: Callable[[], None],
        on_finish: Callable[[], None],
        on_cancel: Callable[[], None],
        label: str = "Capture",
    ) -> bool:
        self._on_capture_start = on_start
        self._on_capture_finish = on_finish
        self._on_capture_cancel = on_cancel
        self._capture_label = label
        self._events.put(("capture_config", (True, label)))
        return True

    def configure_controls(self, controls) -> None:
        self._controls = [dict(control) for control in controls]
        self._events.put(("controls", list(self._controls)))

    def clear_capture(self) -> None:
        self._on_capture_start = None
        self._on_capture_finish = None
        self._on_capture_cancel = None
        self._capture_label = "Capture"
        self._events.put(("capture_config", (False, self._capture_label)))

    def user(self, message: str) -> None:
        self._tool_activities = {}
        self._tool_chain_open = False
        self._events.put(("user", message))

    def assistant(self, token: str) -> None:
        if token and self._tool_activities:
            self._tool_chain_open = False
        self._events.put(("assistant", token))

    def status(self, message: str) -> None:
        self._events.put(("status", message))

    def _begin_tool_chain(self) -> None:
        self._tool_activities = {}
        self._tool_group_id += 1
        self._tool_chain_open = True

    def tool_started(
        self,
        call_id: str,
        name: str,
        arguments: dict,
        description: str | None = None,
    ) -> None:
        if not self._tool_chain_open:
            self._begin_tool_chain()
        self._tool_activities[call_id] = {
            "description": description or f"Running {name}…",
            "state": "running",
        }
        self._emit_tool_group()

    def tool_finished(
        self,
        call_id: str,
        name: str,
        result,
        description: str | None = None,
    ) -> None:
        if call_id not in self._tool_activities and not self._tool_chain_open:
            self._begin_tool_chain()
        state = "failed" if result.is_error else "finished"
        fallback = f"{name} failed" if result.is_error else f"Finished {name}"
        self._tool_activities[call_id] = {
            "description": description or fallback,
            "state": state,
        }
        self._emit_tool_group()

    def _emit_tool_group(self) -> None:
        activities = list(self._tool_activities.values())
        latest = activities[-1]
        state = latest["state"]
        symbols = {"running": "◌", "finished": "✓", "failed": "!"}
        details = "\n".join(
            f"{symbols[activity['state']]}  {activity['description']}"
            for activity in activities
        )
        self._events.put((
            "tool_group",
            (
                self._tool_group_id,
                latest["description"],
                state,
                details,
                len(activities),
            ),
        ))

    def finish(self, interrupted: bool = False) -> None:
        self._events.put(("finish", interrupted))

    def capture_state(self, state: str) -> None:
        self._events.put(("capture_state", state))

    def draft(self, text: str) -> None:
        self._events.put(("draft", text))

    def permission_requested(self, request) -> None:
        arguments = json.dumps(request.arguments, ensure_ascii=False, indent=2)
        effects = ", ".join(request.effects) or "unspecified"
        details = (
            f"Operation: {request.operation_id}\n"
            f"Origin: {request.origin}\n"
            f"Effects: {effects}\n\n"
            f"Arguments:\n{arguments}"
        )
        preview_title = request.preview_title or "Preview"
        preview_content = request.preview_content or ""
        self._events.put((
            "permission",
            (
                request.request_id,
                request.label,
                details,
                preview_title,
                preview_content,
            ),
        ))

    def _resolve_permission(self, request_id: str, allow: bool) -> None:
        if not self._api:
            raise RuntimeError("frontend permission API is unavailable")
        self._api.send(ResolvePermission(request_id, bool(allow)))

    def _drain_events(self):
        while True:
            try:
                yield self._events.get_nowait()
            except queue.Empty:
                return

    def _submit(self, message: str) -> None:
        message = message.strip()
        if message and self._on_submit:
            self._on_submit(message)

    def _interrupt(self) -> None:
        if self._on_interrupt:
            self._on_interrupt()

    def _select_model(self, model: str) -> None:
        choices = {option["id"]: option["name"] for option in self._models}
        if model not in choices:
            raise ValueError("model is unavailable")
        if not self._on_model_select:
            raise ValueError("model selection is unavailable")
        self._on_model_select(model)
        self._selected_model = model
        self._events.put(("model", (model, choices[model])))

    def _apply_modules(self, serialized: str) -> None:
        if self._module_change_busy:
            raise RuntimeError("a module change is already in progress")
        if not self._on_modules_apply:
            raise RuntimeError("module selection is unavailable")
        try:
            requested = json.loads(serialized)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("invalid module selection") from error
        if not isinstance(requested, dict):
            raise ValueError("module selection must be an object")

        categories = {category["id"]: category for category in self._module_catalog}
        if set(requested) - set(categories):
            raise ValueError("module selection contains an unknown capability")
        normalized = {}
        for capability, names in requested.items():
            category = categories[capability]
            if not isinstance(names, list) or any(
                not isinstance(name, str) for name in names
            ):
                raise ValueError(f"invalid selection for {capability}")
            names = list(dict.fromkeys(names))
            available = {option["id"] for option in category["options"]}
            if any(name not in available for name in names):
                raise ValueError(f"unavailable {capability} module")
            if not category["multiple"] and len(names) > 1:
                raise ValueError(f"{capability} accepts one module")
            if not names and not category["optional"] and not category["multiple"]:
                raise ValueError(f"{capability} requires a module")
            normalized[capability] = names

        self._module_change_busy = True
        self._events.put(("modules_busy", (True, "Applying modules…")))

        def apply():
            try:
                self._on_modules_apply(normalized)
            except Exception as error:
                self._events.put(("modules_busy", (False, str(error))))
            else:
                self._events.put(("modules_busy", (False, "")))
            finally:
                self._module_change_busy = False

        self._module_change_thread = threading.Thread(
            target=apply,
            name="kayden-module-change",
            daemon=True,
        )
        self._module_change_thread.start()

    def _capture(self, action: str) -> None:
        callbacks = {
            "start": self._on_capture_start,
            "finish": self._on_capture_finish,
            "cancel": self._on_capture_cancel,
        }
        callback = callbacks.get(action)
        if callback is None:
            raise ValueError("capture action is unavailable")
        callback()

    def _invoke_control(self, control_id: str, value=None) -> None:
        if not self._api:
            raise RuntimeError("frontend control API is unavailable")
        available = {control["id"] for control in self._controls}
        if control_id not in available:
            raise ValueError("control is unavailable")
        self._api.send(InvokeControl(control_id, value))

    def _close_from_ui(self) -> None:
        if self._closed.is_set():
            return
        if self._on_capture_cancel:
            self._on_capture_cancel()
        self._interrupt()
        self._closed.set()

    def run(
        self,
        api,
    ) -> None:
        self._api = api
        self._on_submit = lambda text: api.send(SubmitMessage(text))
        self._on_interrupt = lambda: api.send(InterruptTurn())
        self._unsubscribe = api.subscribe(self._accept_event)
        self._closed.clear()
        try:
            try:
                from .backend import run_qt
            except ModuleNotFoundError as error:
                if error.name == "PySide6" or error.name.startswith("PySide6."):
                    raise RuntimeError(
                        "The Qt frontend requires PySide6. Install it with "
                        "`pip install 'kayden[qt]'`."
                    ) from error
                raise
            run_qt(self)
            if self._module_change_thread and self._module_change_thread.is_alive():
                self._module_change_thread.join()
        finally:
            self._unsubscribe()
            self._unsubscribe = None
            self._api = None

    def stop(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._events.put(("close", None))
