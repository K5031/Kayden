import json
import queue
import threading

from kayden.config import load_system_prompt
from kayden.events import (
    CoreTextDelta,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnRequest,
    UsageReported,
)
from kayden.interfaces import ManualInputInterface
from kayden.klogger import get_logger


log = get_logger("Application")
_STOP = object()

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


def _restore_transcript(frontend, memory):
    open_turn = False
    for message in memory.recent(50):
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if role == "user":
            if open_turn:
                frontend.finish(interrupted=True)
            frontend.user(content)
            open_turn = True
        elif role == "assistant":
            frontend.assistant(content)
            frontend.finish()
            open_turn = False
    if open_turn:
        frontend.finish(interrupted=True)


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

    frontend.status("[context] compacting older conversation")
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
        frontend.status(finished_status)
        return

    failed_markers = ("[request failed", "[api error", "[connection dropped")
    constrain_summary = getattr(context, "constrain_summary", None)
    summary = constrain_summary(summary) if constrain_summary else summary.strip()
    if not interrupted.is_set() and summary and not summary.lower().startswith(failed_markers):
        if memory.commit_compaction(plan, summary):
            context.compacted(memory.view())
            log.info("compacted older conversation into %d characters", len(summary.strip()))
    frontend.status(finished_status)


def run_application(api):
    """Run Kayden's fixed conversation lifecycle over the selected modules."""
    frontend = api.get("frontend")
    core = api.get("core")
    context = api.get("context")
    memory = api.get("memory")
    action = api.get("action")
    tools = api.get("tools")
    speech_input = _optional(api, "input")
    speech_output = _optional(api, "output")
    report_usage = getattr(context, "usage_mode", None) == "reported"

    configure_identity = getattr(frontend, "configure_identity", None)
    if configure_identity:
        configure_identity(getattr(api, "assistant_name", "Kayden"))
    system_prompt = load_system_prompt()
    pending = queue.Queue()
    busy = threading.Event()
    interrupted = threading.Event()
    stopping = threading.Event()
    capture_busy = threading.Event()
    capture_threads = []

    add_tools = getattr(tools, "add", None)
    memory_actions = memory.actions()
    if memory_actions is not None and add_tools:
        add_tools(memory_actions)

    model_name = getattr(core, "model", None) or getattr(core, "model_path", "unknown")
    available_models = getattr(core, "available_models", lambda: [])()

    def select_model(model):
        if busy.is_set():
            raise RuntimeError("cannot change model while a response is active")
        core.select_model(model)
        frontend.status(f"Model: {core.model}")

    configure_models = getattr(frontend, "configure_models", None)
    if configure_models and available_models:
        configure_models(available_models, model_name, select_model)

    _restore_transcript(frontend, memory)
    frontend.status(f"Model: {model_name}")

    def interrupt():
        if not busy.is_set():
            return
        interrupted.set()
        core.interrupt()
        if speech_output:
            speech_output.interrupt()

    def submit(message, interrupt_active=True):
        if not isinstance(message, str) or not message.strip():
            return
        if busy.is_set() and interrupt_active:
            interrupt()
        pending.put(message.strip())

    def start_capture():
        if stopping.is_set() or capture_busy.is_set():
            return
        if busy.is_set():
            interrupt()
        try:
            speech_input.start_capture()
        except Exception as error:
            log.exception("Input capture failed to start")
            frontend.status(f"[error] {error}")
            frontend.capture_state("idle")
            return
        frontend.capture_state("recording")

    def finish_capture():
        if stopping.is_set() or capture_busy.is_set():
            return
        capture_busy.set()
        frontend.capture_state("transcribing")

        def transcribe():
            try:
                text = speech_input.finish_capture()
                if text and not stopping.is_set():
                    frontend.draft(text)
                elif not stopping.is_set():
                    frontend.status("[voice] No speech detected")
            except Exception as error:
                if not stopping.is_set():
                    log.exception("Input transcription failed")
                    frontend.status(f"[error] {error}")
            finally:
                capture_busy.clear()
                if not stopping.is_set():
                    frontend.capture_state("idle")

        thread = threading.Thread(
            target=transcribe,
            name="kayden-input-transcription",
            daemon=True,
        )
        capture_threads.append(thread)
        thread.start()

    def cancel_capture():
        if capture_busy.is_set():
            return
        try:
            speech_input.cancel_capture()
        except Exception:
            log.exception("Input capture failed to cancel")
        frontend.capture_state("idle")

    def process_turn(user_input):
        interrupted.clear()
        frontend.user(user_input)
        if speech_output:
            speech_output.interrupt()

        # Normally compaction happens after the preceding turn. This preflight
        # also protects the first turn after restarting with a nearly-full context.
        _compact_context(context, memory, core, frontend, interrupted, "Thinking…")
        prior_context = context.prepare(memory.view())
        # Persist the user message before the model request so a crash or API
        # failure does not erase what the user said.
        memory.append([{"role": "user", "content": user_input}])
        memories = memory.recall(user_input)
        turn = _build_turn(system_prompt, prior_context, memories, user_input)
        reply = ""
        prompt_tokens = None

        for event in action.run(
            core,
            turn,
            tools,
            should_interrupt=interrupted.is_set,
        ):
            if isinstance(event, TextDelta):
                frontend.assistant(event.text)
                if speech_output:
                    speech_output.send(event.text)
                reply += event.text
            elif isinstance(event, ToolStarted):
                arguments = json.dumps(event.arguments, ensure_ascii=False)
                frontend.status(f"[tool] {event.name}({arguments})")
            elif isinstance(event, ToolFinished):
                if event.result.is_error:
                    frontend.status(
                        f"[tool] {event.name} failed: {event.result.content}"
                    )
                else:
                    frontend.status(f"[tool] {event.name} finished")
            elif isinstance(event, UsageReported):
                reported = event.usage.get("prompt_tokens")
                if isinstance(reported, int):
                    prompt_tokens = reported

        was_interrupted = interrupted.is_set()
        if speech_output:
            if was_interrupted:
                speech_output.interrupt()
            else:
                speech_output.flush()
        frontend.finish(was_interrupted)

        if report_usage and isinstance(prompt_tokens, int):
            context.report_usage(prompt_tokens)

        if reply or not was_interrupted:
            memory.append([{"role": "assistant", "content": reply}])
        _compact_context(context, memory, core, frontend, interrupted)

    def worker():
        while not stopping.is_set():
            user_input = pending.get()
            if user_input is _STOP:
                return
            busy.set()
            try:
                process_turn(user_input)
            except Exception as error:
                log.exception("Turn failed")
                frontend.status(f"[error] {error}")
                frontend.finish(interrupted.is_set())
            finally:
                busy.clear()
                interrupted.clear()

    def speech_worker():
        while not stopping.is_set():
            try:
                submit(speech_input.get_input(), interrupt_active=False)
            except EOFError:
                return
            except Exception:
                if not stopping.is_set():
                    log.exception("Speech input failed")
                return

    manual_input = isinstance(speech_input, ManualInputInterface)
    if manual_input:
        supported = frontend.configure_capture(
            start_capture,
            finish_capture,
            cancel_capture,
            label=speech_input.capture_label,
        )
        if not supported:
            raise RuntimeError(
                f"{type(frontend).__name__} does not support manual input capture"
            )

    turn_thread = threading.Thread(target=worker, name="kayden-turns", daemon=True)
    turn_thread.start()
    if speech_input and not manual_input:
        threading.Thread(
            target=speech_worker,
            name="kayden-speech-input",
            daemon=True,
        ).start()

    try:
        frontend.run(submit, interrupt)
    finally:
        stopping.set()
        if manual_input:
            try:
                speech_input.cancel_capture()
            except Exception:
                log.exception("Input capture failed to stop")
        interrupt()
        pending.put(_STOP)
        turn_thread.join(timeout=2)
        for thread in capture_threads:
            thread.join(timeout=2)
