import json
import queue
import threading

from kayden.config import load_system_prompt, save_module_selections
from kayden.contributions import ControlContribution
from kayden.events import (
    CoreTextDelta,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnRequest,
    UsageReported,
)
from kayden.frontend_api import (
    ApplyModuleSelections,
    AssistantText,
    CancelCapture,
    CaptureConfigured,
    CaptureStateChanged,
    ControlsConfigured,
    DraftChanged,
    FinishCapture,
    FrontendAPI,
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
from kayden.interfaces import FrontendInterface, ManualInputInterface
from kayden.klogger import get_logger
from kayden.permissions import (
    InvocationContext,
    OperationDefinition,
    PermissionBroker,
)


log = get_logger("Application")
_STOP = object()

SWAPPABLE_CAPABILITIES = (
    "core",
    "context",
    "memory",
    "action",
    "input",
    "output",
    "tools",
)
MULTI_SELECT_CAPABILITIES = {"output", "tools"}

COMPACTION_PROMPT = """Condense the earlier conversation into a compact factual state for continuing it later.

Preserve:
- user preferences, decisions, corrections, and ongoing goals
- project names, paths, technical details, and unresolved work
- commitments made by the assistant
- enough context to understand references in the recent conversation

Do not add facts or commentary. Do not describe the summarization task. Use concise bullets with clear labels."""


def _optional(api, capability):
    return api.get(capability) if api.has(capability) else None


def _build_turn(system_prompt, context, memories, user_input):
    if memories:
        system_prompt += f"\n\nRelevant memories about the user:\n{memories}"
    return TurnRequest(messages=[
        {"role": "system", "content": system_prompt},
        *context,
        {"role": "user", "content": user_input},
    ])


def _publish(frontend, event):
    """Publish to the API, with compatibility for directly tested helpers."""
    publish = getattr(frontend, "_publish", None)
    if publish:
        publish(event)
    elif isinstance(event, UserMessage):
        frontend.user(event.text)
    elif isinstance(event, AssistantText):
        frontend.assistant(event.text)
    elif isinstance(event, StatusChanged):
        frontend.status(event.text)
    elif isinstance(event, TurnFinished):
        frontend.finish(event.interrupted)
    else:
        raise TypeError(f"frontend cannot receive {type(event).__name__}")


def _restore_transcript(frontend, memory):
    open_turn = False
    for message in memory.recent(50):
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if role == "user":
            if open_turn:
                _publish(frontend, TurnFinished(interrupted=True))
            _publish(frontend, UserMessage(content))
            open_turn = True
        elif role == "assistant":
            _publish(frontend, AssistantText(content))
            _publish(frontend, TurnFinished())
            open_turn = False
    if open_turn:
        _publish(frontend, TurnFinished(interrupted=True))


def _compact_context(
    context,
    memory,
    core,
    frontend,
    interrupted,
    finished_status="Ready",
):
    if interrupted.is_set():
        return
    view = memory.view()
    if not context.needs_compaction(view):
        return
    retention = getattr(context, "retained_message_count", None)
    keep_recent = (
        retention(view)
        if retention
        else min(len(view.messages), max(2, int(context.keep_recent_messages)))
    )
    plan = memory.plan_compaction(keep_recent)
    if plan is None:
        return

    _publish(frontend, StatusChanged("[context] compacting older conversation"))
    summary = ""
    source = []
    if plan.previous_summary:
        source.append({
            "role": "system",
            "content": "Existing summary:\n" + plan.previous_summary,
        })
    source.extend(plan.messages)
    summary_prompt = COMPACTION_PROMPT
    summary_target = getattr(context, "summary_target_tokens", None)
    if isinstance(summary_target, int) and summary_target > 0:
        summary_prompt += (
            f"\n\nKeep the resulting summary below {summary_target} tokens. "
            "Rewrite and consolidate existing points rather than appending to them."
        )
    try:
        for event in core.stream(
            [{"role": "system", "content": summary_prompt}, *source],
            tools=None,
            should_interrupt=interrupted.is_set,
        ):
            if isinstance(event, CoreTextDelta):
                summary += event.text
    except Exception:
        log.exception("Context compaction failed")
        _publish(frontend, StatusChanged(finished_status))
        return

    failed_markers = ("[request failed", "[api error", "[connection dropped")
    constrain_summary = getattr(context, "constrain_summary", None)
    summary = constrain_summary(summary) if constrain_summary else summary.strip()
    if not interrupted.is_set() and summary and not summary.lower().startswith(failed_markers):
        if memory.commit_compaction(plan, summary):
            context.compacted(memory.view())
            log.info("compacted older conversation into %d characters", len(summary.strip()))
    _publish(frontend, StatusChanged(finished_status))


class _LegacyFrontendAdapter:
    """Temporary compatibility for frontends using the pre-API contract."""

    def __init__(self, frontend, api):
        self.frontend = frontend
        self.api = api
        self.unsubscribe = None

    def _accept(self, event):
        if isinstance(event, IdentityConfigured):
            callback = getattr(self.frontend, "configure_identity", None)
            if callback:
                callback(event.assistant_name)
        elif isinstance(event, ModelsConfigured):
            callback = getattr(self.frontend, "configure_models", None)
            if callback:
                callback(
                    event.models,
                    event.selected_model,
                    lambda model: self.api.send(SelectModel(model)),
                )
        elif isinstance(event, ModulesConfigured):
            callback = getattr(self.frontend, "configure_modules", None)
            if callback:
                callback(
                    event.modules,
                    lambda selections: self.api.send(
                        ApplyModuleSelections(selections)
                    ),
                )
        elif isinstance(event, CaptureConfigured):
            if event.available:
                callback = getattr(self.frontend, "configure_capture", None)
                if callback:
                    callback(
                        lambda: self.api.send(StartCapture()),
                        lambda: self.api.send(FinishCapture()),
                        lambda: self.api.send(CancelCapture()),
                        event.label,
                    )
            else:
                callback = getattr(self.frontend, "clear_capture", None)
                if callback:
                    callback()
        elif isinstance(event, UserMessage):
            self.frontend.user(event.text)
        elif isinstance(event, AssistantText):
            self.frontend.assistant(event.text)
        elif isinstance(event, StatusChanged):
            self.frontend.status(event.text)
        elif isinstance(event, ToolActivityStarted):
            callback = getattr(self.frontend, "tool_started", None)
            if callback:
                callback(
                    event.call_id,
                    event.name,
                    event.arguments,
                    event.description,
                )
            else:
                arguments = json.dumps(event.arguments, ensure_ascii=False)
                self.frontend.status(f"[tool] {event.name}({arguments})")
        elif isinstance(event, ToolActivityFinished):
            callback = getattr(self.frontend, "tool_finished", None)
            if callback:
                callback(
                    event.call_id,
                    event.name,
                    event.result,
                    event.description,
                )
            elif event.result.is_error:
                self.frontend.status(
                    f"[tool] {event.name} failed: {event.result.content}"
                )
            else:
                self.frontend.status(f"[tool] {event.name} finished")
        elif isinstance(event, TurnFinished):
            self.frontend.finish(event.interrupted)
        elif isinstance(event, CaptureStateChanged):
            callback = getattr(self.frontend, "capture_state", None)
            if callback:
                callback(event.state)
        elif isinstance(event, DraftChanged):
            callback = getattr(self.frontend, "draft", None)
            if callback:
                callback(event.text)
        elif isinstance(event, PermissionRequested):
            callback = getattr(self.frontend, "request_permission", None)
            allowed = bool(callback(event)) if callback else False
            self.api.send(ResolvePermission(event.request_id, allowed))

    def run(self):
        self.unsubscribe = self.api.subscribe(self._accept)
        try:
            self.frontend.run(
                lambda text: self.api.send(SubmitMessage(text)),
                lambda: self.api.send(InterruptTurn()),
            )
        finally:
            self.unsubscribe()


class ApplicationRuntime:
    """Own Kayden's lifetime while presentation modules attach and detach."""

    def __init__(self, api, owns_registry=False):
        self.api = api
        self.owns_registry = owns_registry
        self.system_prompt = load_system_prompt()
        self.pending = queue.Queue()
        self.busy = threading.Event()
        self.interrupted = threading.Event()
        self.stopping = threading.Event()
        self.capture_busy = threading.Event()
        self.capture_threads = []
        self.control_threads = []
        self.turn_thread = None
        self.speech_thread = None
        self._started = False
        self._closed = False
        self._frontend = None
        self._frontend_condition = threading.Condition()
        self._idle_condition = threading.Condition()
        self._turn_gate = threading.Lock()
        self._memory_actions = None
        self._control_handlers = {}
        self._permission_lock = threading.RLock()
        self._permission_prompt_lock = threading.Lock()
        self._permission_waiters = {}
        self.permissions = PermissionBroker(
            ask=self._ask_permission,
            rules=getattr(api, "permission_rules", {}),
        )

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def is_running(self):
        return self._started and not self.stopping.is_set()

    def _current_frontend(self):
        with self._frontend_condition:
            return self._frontend

    def _wait_for_frontend(self):
        with self._frontend_condition:
            while self._frontend is None and not self.stopping.is_set():
                self._frontend_condition.wait()
            return self._frontend

    def wait_until_idle(self):
        with self._idle_condition:
            while self.busy.is_set() and not self.stopping.is_set():
                self._idle_condition.wait()

    def start(self):
        if self._closed:
            raise RuntimeError("runtime is closed")
        if self._started:
            return

        memory = self.api.get("memory")
        tools = self.api.get("tools")
        self._bind_tools(tools)
        add_tools = getattr(tools, "add", None)
        memory_actions = memory.actions()
        if memory_actions is not None and add_tools:
            add_tools(memory_actions)
        self._memory_actions = memory_actions

        self.turn_thread = threading.Thread(
            target=self._worker,
            name="kayden-turns",
            daemon=True,
        )
        self.turn_thread.start()

        speech_input = _optional(self.api, "input")
        if speech_input and not isinstance(speech_input, ManualInputInterface):
            self.speech_thread = threading.Thread(
                target=self._speech_worker,
                args=(speech_input,),
                name="kayden-speech-input",
                daemon=True,
            )
            self.speech_thread.start()
        self._started = True

    def attach_frontend(self, frontend):
        """Run one frontend attachment without giving it runtime ownership."""
        self.start()
        self.wait_until_idle()
        connection = FrontendAPI(self._handle_frontend_command)
        with self._frontend_condition:
            if self._frontend is not None:
                raise RuntimeError("a frontend is already attached")
            self._frontend = connection
            self._frontend_condition.notify_all()

        try:
            self._configure_frontend(connection)
            if (
                isinstance(frontend, FrontendInterface)
                and getattr(frontend, "frontend_api_version", 0) == 1
            ):
                frontend.run(connection)
            else:
                _LegacyFrontendAdapter(frontend, connection).run()
        finally:
            self._cancel_permission_requests()
            with self._frontend_condition:
                if self._frontend is connection:
                    self._frontend = None
                self._frontend_condition.notify_all()
            connection._close()

    def _handle_frontend_command(self, command):
        if isinstance(command, SubmitMessage):
            self.submit(command.text)
        elif isinstance(command, InterruptTurn):
            self.interrupt()
        elif isinstance(command, SelectModel):
            self.select_model(command.model)
        elif isinstance(command, ApplyModuleSelections):
            self.apply_module_selections(command.selections)
        elif isinstance(command, StartCapture):
            self.start_capture()
        elif isinstance(command, FinishCapture):
            self.finish_capture()
        elif isinstance(command, CancelCapture):
            self.cancel_capture()
        elif isinstance(command, InvokeControl):
            thread = threading.Thread(
                target=self._invoke_control_from_frontend,
                args=(command.control_id, command.value),
                name="kayden-control",
                daemon=True,
            )
            self.control_threads.append(thread)
            thread.start()
        elif isinstance(command, ResolvePermission):
            self.resolve_permission(command.request_id, command.allow)
        else:
            raise TypeError(f"unsupported frontend command: {type(command).__name__}")

    def _configure_frontend(self, frontend):
        memory = self.api.get("memory")
        frontend._publish(IdentityConfigured(
            getattr(self.api, "assistant_name", "Kayden")
        ))

        self._configure_models(frontend)
        self._configure_modules(frontend)

        _restore_transcript(frontend, memory)

        speech_input = _optional(self.api, "input")
        if isinstance(speech_input, ManualInputInterface):
            self._configure_capture(frontend, speech_input)
        else:
            frontend._publish(CaptureConfigured(False))
        self._refresh_controls(frontend)

    def _refresh_controls(self, frontend=None):
        controls = []
        handlers = {}
        for capability in SWAPPABLE_CAPABILITIES:
            if not self.api.has(capability):
                continue
            module = self.api.get(capability)
            provide = getattr(module, "contributions", None)
            if not provide:
                continue
            provided = provide()
            if provided is None:
                continue
            for contribution in provided:
                if not isinstance(contribution, ControlContribution):
                    raise TypeError(
                        f"{capability} contributed unsupported value "
                        f"{type(contribution).__name__}"
                    )
                if contribution.id in handlers:
                    raise ValueError(
                        f"duplicate contribution id: {contribution.id}"
                    )
                handlers[contribution.id] = contribution
                controls.append(contribution.descriptor())
        self._control_handlers = handlers
        frontend = frontend or self._current_frontend()
        if frontend:
            frontend._publish(ControlsConfigured(controls))

    def invoke_control(self, control_id, value=None):
        if not isinstance(control_id, str) or not control_id:
            raise ValueError("control id must be non-empty text")
        try:
            contribution = self._control_handlers[control_id]
        except KeyError as error:
            raise ValueError(f"control is unavailable: {control_id}") from error
        operation = contribution.operation or OperationDefinition(
            id=f"control.{contribution.id}",
            label=contribution.label,
        )
        self.permissions.require(
            operation,
            {"value": value},
            InvocationContext(origin="user", explicit=True),
        )
        contribution.invoke(value)
        self._refresh_controls()

    def _invoke_control_from_frontend(self, control_id, value):
        try:
            self.invoke_control(control_id, value)
        except Exception as error:
            log.exception("Frontend control failed: %s", control_id)
            frontend = self._current_frontend()
            if frontend:
                frontend._publish(StatusChanged(f"[error] {error}"))

    def _bind_tools(self, tools):
        bind = getattr(tools, "bind_authorizer", None)
        if bind:
            bind(lambda operation, arguments: self.permissions.require(
                operation,
                arguments,
                InvocationContext(origin="model", explicit=False),
            ))

    def _ask_permission(self, request):
        # Frontends present one modal request at a time. Serialize prompts here
        # so concurrent module activity cannot overwrite an unanswered request.
        with self._permission_prompt_lock:
            frontend = self._current_frontend()
            if frontend is None or self.stopping.is_set():
                return False
            waiter = threading.Event()
            state = {"allow": False}
            with self._permission_lock:
                self._permission_waiters[request.request_id] = (waiter, state)
            frontend._publish(PermissionRequested(
                request_id=request.request_id,
                operation_id=request.operation.id,
                label=request.operation.label,
                arguments=request.arguments,
                effects=tuple(sorted(request.operation.effects)),
                origin=request.context.origin,
                explicit=request.context.explicit,
            preview_kind=(
                request.operation.preview.kind
                if request.operation.preview else None
            ),
            preview_title=(
                request.operation.preview.title
                if request.operation.preview else None
            ),
            preview_content=(
                request.operation.preview.content
                if request.operation.preview else None
            ),
            ))
            try:
                while not waiter.wait(0.1):
                    if self.stopping.is_set():
                        return False
                    if (
                        request.context.origin == "model"
                        and self.interrupted.is_set()
                    ):
                        return False
                return state["allow"]
            finally:
                with self._permission_lock:
                    self._permission_waiters.pop(request.request_id, None)

    def resolve_permission(self, request_id, allow):
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("permission request id must be non-empty text")
        if not isinstance(allow, bool):
            raise TypeError("permission response must be boolean")
        with self._permission_lock:
            pending = self._permission_waiters.get(request_id)
        if pending is None:
            raise ValueError("permission request is no longer active")
        waiter, state = pending
        state["allow"] = allow
        waiter.set()

    def _cancel_permission_requests(self):
        with self._permission_lock:
            pending = list(self._permission_waiters.values())
        for waiter, _state in pending:
            waiter.set()

    def _configure_models(self, frontend):
        core = self.api.get("core")
        model_name = (
            getattr(core, "model", None)
            or getattr(core, "model_path", "unknown")
        )
        available_models = getattr(core, "available_models", lambda: [])()
        if available_models:
            frontend._publish(ModelsConfigured(available_models, model_name))
        frontend._publish(StatusChanged(f"Model: {model_name}"))

    def _configure_modules(self, frontend):
        discover = getattr(self.api, "available_modules", None)
        selected = getattr(self.api, "selection", None)
        if not discover or not selected:
            return
        catalog = []
        for capability in SWAPPABLE_CAPABILITIES:
            choices = discover(capability)
            current = selected(capability)
            current = current if isinstance(current, list) else ([current] if current else [])
            catalog.append({
                "id": capability,
                "name": capability.replace("_", " ").title(),
                "multiple": capability in MULTI_SELECT_CAPABILITIES,
                "optional": capability in {"input", "output"},
                "options": choices,
                "selected": current,
            })
        frontend._publish(ModulesConfigured(catalog))

    def _configure_capture(self, frontend, speech_input):
        frontend._publish(CaptureConfigured(True, speech_input.capture_label))

    @staticmethod
    def _stop_module(module):
        if module is None:
            return
        stop = getattr(module, "stop", None)
        if stop:
            try:
                stop()
            except Exception:
                log.exception("Failed to stop displaced module: %s", module)

    def swap_modules(self, replacements: dict):
        """Publish prepared module instances at a boundary between turns."""
        if not hasattr(self.api, "swap"):
            raise TypeError("the active module registry does not support swapping")
        if "frontend" in replacements and self._current_frontend() is not None:
            raise RuntimeError("detach the active frontend before replacing it")

        replacement_memory = replacements.get("memory", self.api.get("memory"))
        replacement_tools = replacements.get("tools", self.api.get("tools"))
        self._bind_tools(replacement_tools)
        replacement_actions = replacement_memory.actions()
        if "tools" in replacements and replacement_actions is not None:
            add_tools = getattr(replacement_tools, "add", None)
            if add_tools:
                add_tools(replacement_actions)

        with self._turn_gate:
            displaced = self.api.swap(replacements)
            try:
                if "memory" in replacements and "tools" not in replacements:
                    replace_borrowed = getattr(
                        replacement_tools,
                        "replace_borrowed",
                        None,
                    )
                    if replace_borrowed:
                        replace_borrowed(self._memory_actions, replacement_actions)
                if "memory" in replacements:
                    self._memory_actions = replacement_actions
            except Exception:
                self.api.swap(displaced)
                raise

        old_input = displaced.get("input") if "input" in replacements else None
        if "input" in replacements:
            self._stop_module(old_input)
            if self.speech_thread:
                self.speech_thread.join(timeout=2)
                self.speech_thread = None
            new_input = replacements["input"]
            if new_input and not isinstance(new_input, ManualInputInterface):
                self.speech_thread = threading.Thread(
                    target=self._speech_worker,
                    args=(new_input,),
                    name="kayden-speech-input",
                    daemon=True,
                )
                self.speech_thread.start()

        frontend = self._current_frontend()
        if frontend and "core" in replacements:
            try:
                self._configure_models(frontend)
            except Exception:
                log.exception("Failed to refresh frontend model controls")
        if frontend and "input" in replacements:
            new_input = replacements["input"]
            if isinstance(new_input, ManualInputInterface):
                try:
                    self._configure_capture(frontend, new_input)
                except Exception:
                    log.exception("Failed to refresh frontend capture controls")
            else:
                frontend._publish(CaptureConfigured(False))

        if frontend:
            try:
                self._refresh_controls(frontend)
            except Exception:
                log.exception("Failed to refresh frontend contributions")
                self._control_handlers = {}
                frontend._publish(ControlsConfigured([]))

        for capability, old_module in displaced.items():
            if capability == "input" or old_module is replacements.get(capability):
                continue
            self._stop_module(old_module)
        return displaced

    def swap_configured(self, selections: dict, settings=None):
        """Prepare named modules, then swap them without disturbing failures."""
        if not isinstance(selections, dict) or not selections:
            raise TypeError("selections must be a non-empty mapping")
        settings = {} if settings is None else settings
        if not isinstance(settings, dict):
            raise TypeError("settings must be a mapping")
        prepare = getattr(self.api, "prepare", None)
        if prepare is None:
            raise TypeError("the active module registry cannot prepare modules")

        candidates = {}
        try:
            for capability, names in selections.items():
                if capability in {"input", "output"} and not names:
                    candidates[capability] = None
                    continue
                candidates[capability] = prepare(
                    capability,
                    names,
                    settings.get(capability, {}),
                )
            displaced = self.swap_modules(candidates)
            set_selections = getattr(self.api, "set_selections", None)
            if set_selections:
                set_selections(selections)
            return displaced
        except Exception:
            for candidate in reversed(list(candidates.values())):
                try:
                    self._stop_module(candidate)
                except Exception:
                    log.exception("Failed to stop rejected module: %s", candidate)
            raise

    def apply_module_selections(self, selections: dict):
        """Validate, atomically apply, and persist a generic UI selection set."""
        if not isinstance(selections, dict):
            raise TypeError("module selections must be a mapping")
        unknown = set(selections) - set(SWAPPABLE_CAPABILITIES)
        if unknown:
            raise ValueError(f"unsupported module capabilities: {sorted(unknown)}")

        changes = {}
        for capability, requested in selections.items():
            names = requested if isinstance(requested, list) else [requested]
            if any(not isinstance(name, str) or not name for name in names):
                raise ValueError(f"invalid {capability} module selection")
            if capability not in MULTI_SELECT_CAPABILITIES and len(names) > 1:
                raise ValueError(f"{capability} accepts only one module")
            if not names and capability not in {"input", "output", "tools"}:
                raise ValueError(f"{capability} requires a module")
            current = getattr(self.api, "selection")(capability)
            current_names = current if isinstance(current, list) else ([current] if current else [])
            if names != current_names:
                changes[capability] = names if capability in MULTI_SELECT_CAPABILITIES else (names[0] if names else [])

        if not changes:
            return

        configured_settings = getattr(self.api, "module_settings", {})
        prepared_settings = {}
        for capability, selected in changes.items():
            names = selected if isinstance(selected, list) else ([selected] if selected else [])
            capability_settings = configured_settings.get(capability, {})
            if len(names) == 1:
                prepared_settings[capability] = capability_settings.get(names[0], {})
            elif len(names) > 1:
                prepared_settings[capability] = {
                    name: capability_settings.get(name, {}) for name in names
                }

        self.swap_configured(changes, prepared_settings)
        config_path = getattr(self.api, "config_path", None)
        if config_path:
            persisted = {
                capability: (
                    None
                    if capability in {"input", "output"} and selected == []
                    else selected
                )
                for capability, selected in changes.items()
            }
            save_module_selections(persisted, path=config_path)

        frontend = self._current_frontend()
        if frontend:
            self._configure_modules(frontend)
            frontend._publish(StatusChanged("Modules updated"))

    def select_model(self, model):
        if self.busy.is_set():
            raise RuntimeError("cannot change model while a response is active")
        core = self.api.get("core")
        core.select_model(model)
        frontend = self._current_frontend()
        if frontend:
            frontend._publish(StatusChanged(f"Model: {core.model}"))

    def interrupt(self):
        if not self.busy.is_set():
            return
        self.interrupted.set()
        self.api.get("core").interrupt()
        speech_output = _optional(self.api, "output")
        if speech_output:
            speech_output.interrupt()

    def submit(self, message, interrupt_active=True):
        if not isinstance(message, str) or not message.strip():
            return
        if self.busy.is_set() and interrupt_active:
            self.interrupt()
        self.pending.put(message.strip())

    def start_capture(self):
        speech_input = _optional(self.api, "input")
        frontend = self._current_frontend()
        if (
            speech_input is None
            or frontend is None
            or self.stopping.is_set()
            or self.capture_busy.is_set()
        ):
            return
        if self.busy.is_set():
            self.interrupt()
        try:
            speech_input.start_capture()
        except Exception as error:
            log.exception("Input capture failed to start")
            frontend._publish(StatusChanged(f"[error] {error}"))
            frontend._publish(CaptureStateChanged("idle"))
            return
        frontend._publish(CaptureStateChanged("recording"))

    def finish_capture(self):
        speech_input = _optional(self.api, "input")
        frontend = self._current_frontend()
        if (
            speech_input is None
            or frontend is None
            or self.stopping.is_set()
            or self.capture_busy.is_set()
        ):
            return
        self.capture_busy.set()
        frontend._publish(CaptureStateChanged("transcribing"))

        def transcribe():
            try:
                text = speech_input.finish_capture()
                if text and not self.stopping.is_set():
                    frontend._publish(DraftChanged(text))
                elif not self.stopping.is_set():
                    frontend._publish(StatusChanged("[voice] No speech detected"))
            except Exception as error:
                if not self.stopping.is_set():
                    log.exception("Input transcription failed")
                    frontend._publish(StatusChanged(f"[error] {error}"))
            finally:
                self.capture_busy.clear()
                if not self.stopping.is_set():
                    frontend._publish(CaptureStateChanged("idle"))

        thread = threading.Thread(
            target=transcribe,
            name="kayden-input-transcription",
            daemon=True,
        )
        self.capture_threads.append(thread)
        thread.start()

    def cancel_capture(self):
        speech_input = _optional(self.api, "input")
        if speech_input is None or self.capture_busy.is_set():
            return
        try:
            speech_input.cancel_capture()
        except Exception:
            log.exception("Input capture failed to cancel")
        frontend = self._current_frontend()
        if frontend:
            frontend._publish(CaptureStateChanged("idle"))

    def _process_turn(self, user_input, frontend):
        core = self.api.get("core")
        context = self.api.get("context")
        memory = self.api.get("memory")
        action = self.api.get("action")
        tools = self.api.get("tools")
        speech_output = _optional(self.api, "output")
        report_usage = getattr(context, "usage_mode", None) == "reported"

        self.interrupted.clear()
        frontend._publish(UserMessage(user_input))
        if speech_output:
            speech_output.interrupt()

        _compact_context(
            context,
            memory,
            core,
            frontend,
            self.interrupted,
            "Thinking…",
        )
        prior_context = context.prepare(memory.view())
        memory.append([{"role": "user", "content": user_input}])
        memories = memory.recall(user_input)
        turn = _build_turn(self.system_prompt, prior_context, memories, user_input)
        reply = ""
        prompt_tokens = None

        for event in action.run(
            core,
            turn,
            tools,
            should_interrupt=self.interrupted.is_set,
        ):
            if isinstance(event, TextDelta):
                frontend._publish(AssistantText(event.text))
                if speech_output:
                    speech_output.send(event.text)
                reply += event.text
            elif isinstance(event, ToolStarted):
                frontend._publish(ToolActivityStarted(
                    event.call_id,
                    event.name,
                    event.arguments,
                    event.description,
                ))
            elif isinstance(event, ToolFinished):
                frontend._publish(ToolActivityFinished(
                    event.call_id,
                    event.name,
                    event.result,
                    event.description,
                ))
            elif isinstance(event, UsageReported):
                reported = event.usage.get("prompt_tokens")
                if isinstance(reported, int):
                    prompt_tokens = reported

        was_interrupted = self.interrupted.is_set()
        if speech_output:
            if was_interrupted:
                speech_output.interrupt()
            else:
                speech_output.flush()
        frontend._publish(TurnFinished(was_interrupted))

        if report_usage and isinstance(prompt_tokens, int):
            context.report_usage(prompt_tokens)
        if reply or not was_interrupted:
            memory.append([{"role": "assistant", "content": reply}])
        _compact_context(context, memory, core, frontend, self.interrupted)

    def _worker(self):
        while not self.stopping.is_set():
            user_input = self.pending.get()
            if user_input is _STOP:
                return
            frontend = self._wait_for_frontend()
            if frontend is None:
                return
            with self._turn_gate:
                self.busy.set()
                try:
                    self._process_turn(user_input, frontend)
                except Exception as error:
                    log.exception("Turn failed")
                    frontend._publish(StatusChanged(f"[error] {error}"))
                    frontend._publish(TurnFinished(self.interrupted.is_set()))
                finally:
                    self.busy.clear()
                    self.interrupted.clear()
                    with self._idle_condition:
                        self._idle_condition.notify_all()

    def _speech_worker(self, speech_input):
        while not self.stopping.is_set():
            try:
                self.submit(speech_input.get_input(), interrupt_active=False)
            except EOFError:
                return
            except Exception:
                if not self.stopping.is_set():
                    log.exception("Speech input failed")
                return

    def stop(self):
        if not self._started or self.stopping.is_set():
            return
        self.stopping.set()
        self._cancel_permission_requests()
        with self._frontend_condition:
            self._frontend_condition.notify_all()
        with self._idle_condition:
            self._idle_condition.notify_all()

        speech_input = _optional(self.api, "input")
        if isinstance(speech_input, ManualInputInterface):
            try:
                speech_input.cancel_capture()
            except Exception:
                log.exception("Input capture failed to stop")
        self.interrupt()
        self.pending.put(_STOP)
        if self.turn_thread:
            self.turn_thread.join(timeout=2)
        for thread in self.capture_threads:
            thread.join(timeout=2)
        for thread in self.control_threads:
            thread.join(timeout=2)

    def close(self):
        if self._closed:
            return
        self.stop()
        if self.owns_registry:
            self.api.close()
        self._closed = True


def run_application(api):
    """Compatibility entrypoint that runs the configured frontend until exit."""
    runtime = ApplicationRuntime(api)
    try:
        runtime.attach_frontend(api.get("frontend"))
    finally:
        runtime.close()
