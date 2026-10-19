import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

from kayden.events import (
    CoreTextDelta,
    TextDelta,
    ToolFinished,
    ToolResult,
    ToolStarted,
    UsageReported,
)
from kayden.runtime.application import (
    ApplicationRuntime,
    _compact_context,
    _restore_transcript,
    run_application,
)
from kayden.interfaces import (
    CompactionPlan,
    CoreInterface,
    InputInterface,
    ManualInputInterface,
    MemoryInterface,
    MemoryView,
    ToolInterface,
)
from kayden.runtime.registry import Registry
from kayden.runtime.tools import ToolRuntime


class _Frontend:
    def __init__(self):
        self.events = []
        self.done = threading.Event()

    def run(self, on_submit, on_interrupt):
        on_submit("question")
        if not self.done.wait(timeout=2):
            raise AssertionError("application did not finish the turn")

    def user(self, message):
        self.events.append(("user", message))

    def assistant(self, text):
        self.events.append(("assistant", text))

    def status(self, message):
        self.events.append(("status", message))

    def finish(self, interrupted=False):
        self.events.append(("finish", interrupted))
        self.done.set()


class _Context:
    usage_mode = "reported"
    keep_recent_messages = 12

    def __init__(self):
        self.reported_usage = []

    def prepare(self, view):
        return list(view.messages)

    def needs_compaction(self, view):
        return False

    def compacted(self, view):
        pass

    def report_usage(self, usage):
        self.reported_usage.append(usage)


class _SelfCountingContext(_Context):
    usage_mode = "self"


class _Memory:
    def __init__(self):
        self.queries = []
        self.recorded = []
        self.messages = [{"role": "assistant", "content": "earlier"}]

    def view(self):
        return MemoryView("", list(self.messages))

    def append(self, messages):
        self.recorded.extend(messages)
        self.messages.extend(messages)

    def recall(self, query):
        self.queries.append(query)
        return "likes concise answers"

    def recent(self, limit=50):
        return []

    def actions(self):
        return None


class _Action:
    def __init__(self):
        self.calls = []

    def run(self, core, turn, tools, should_interrupt=None):
        self.calls.append((core, turn, tools, should_interrupt))
        yield ToolStarted("call_1", "current_time", {})
        yield ToolFinished(
            "call_1", "current_time", ToolResult("Friday, 12:00")
        )
        yield ToolFinished(
            "call_2", "broken_tool", ToolResult("Tool error: bad input", True)
        )
        yield TextDelta("hello ")
        yield UsageReported({"prompt_tokens": 17})
        yield TextDelta("world")


class _BlockingAction:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.core = None

    def run(self, core, turn, tools, should_interrupt=None):
        self.core = core
        self.started.set()
        if not self.release.wait(timeout=2):
            raise AssertionError("blocked turn was not released")
        yield TextDelta("finished")


class _InterruptingFrontend(_Frontend):
    def __init__(self, action):
        super().__init__()
        self.action = action
        self.finishes = 0

    def run(self, on_submit, on_interrupt):
        on_submit("first")
        if not self.action.started.wait(timeout=2):
            raise AssertionError("first turn did not start")
        on_submit("second")
        if not self.done.wait(timeout=2):
            raise AssertionError("queued turn did not finish")

    def finish(self, interrupted=False):
        self.events.append(("finish", interrupted))
        self.finishes += 1
        if self.finishes == 2:
            self.done.set()


class _InterruptibleAction:
    def __init__(self):
        self.started = threading.Event()
        self.calls = 0

    def run(self, core, turn, tools, should_interrupt=None):
        self.calls += 1
        if self.calls == 1:
            yield TextDelta("partial")
            self.started.set()
            deadline = time.monotonic() + 2
            while not should_interrupt() and time.monotonic() < deadline:
                time.sleep(0.001)
            return
        yield TextDelta("second reply")


class _InterruptibleCore:
    model = "test-model"

    def __init__(self):
        self.interruptions = 0

    def interrupt(self):
        self.interruptions += 1


class _SwappableCore(CoreInterface):
    reports_usage = True

    def __init__(self, model):
        self.model = model
        self.stopped = False

    def stream(self, messages, tools=None, should_interrupt=None):
        return iter(())

    def interrupt(self):
        pass

    def stop(self):
        self.stopped = True


class _MemoryActions(ToolInterface):
    def __init__(self, value):
        self.value = value

    def schemas(self):
        return [{
            "type": "function",
            "function": {
                "name": "memory_value",
                "parameters": {"type": "object"},
            },
        }]

    def execute(self, name, arguments):
        return self.value


class _SwappableMemory(MemoryInterface):
    def __init__(self, value):
        self.messages = []
        self._actions = _MemoryActions(value)
        self.stopped = False

    def append(self, messages):
        self.messages.extend(dict(message) for message in messages)

    def recall(self, query):
        return ""

    def view(self):
        return MemoryView("", list(self.messages))

    def actions(self):
        return self._actions

    def stop(self):
        self.stopped = True


class _SwappableInput(InputInterface):
    def __init__(self):
        self.values = queue.Queue()
        self.stopped = False

    def get_input(self):
        value = self.values.get()
        if value is None:
            raise EOFError
        return value

    def has_input(self):
        return not self.values.empty()

    def stop(self):
        self.stopped = True
        self.values.put(None)


class _SpeechFrontend(_Frontend):
    def run(self, on_submit, on_interrupt):
        if not self.done.wait(timeout=2):
            raise AssertionError("speech input did not finish the turn")


class _PassiveFrontend(_Frontend):
    def run(self, on_submit, on_interrupt):
        return


class _SpeechInput:
    def __init__(self):
        self.reads = 0

    def get_input(self):
        self.reads += 1
        if self.reads == 1:
            return "spoken question"
        raise EOFError


class _SpeechOutput:
    def __init__(self):
        self.events = []

    def interrupt(self):
        self.events.append(("interrupt", None))

    def send(self, text):
        self.events.append(("send", text))

    def flush(self):
        self.events.append(("flush", None))


class _ManualInput(ManualInputInterface):
    capture_label = "Mic"

    def __init__(self):
        self.events = []

    def start_capture(self):
        self.events.append("start")

    def finish_capture(self):
        self.events.append("finish")
        return "drafted speech"

    def cancel_capture(self):
        self.events.append("cancel")


class _ManualFrontend(_Frontend):
    def __init__(self):
        super().__init__()
        self.capture_events = []
        self.draft_ready = threading.Event()

    def configure_capture(self, on_start, on_finish, on_cancel, label="Capture"):
        self.capture_events.append(("configured", label))
        self.on_capture_start = on_start
        self.on_capture_finish = on_finish
        self.on_capture_cancel = on_cancel
        return True

    def capture_state(self, state):
        self.capture_events.append(("state", state))

    def draft(self, text):
        self.capture_events.append(("draft", text))
        self.draft_ready.set()

    def run(self, on_submit, on_interrupt):
        self.on_capture_start()
        self.on_capture_finish()
        if not self.draft_ready.wait(timeout=2):
            raise AssertionError("dictation did not reach the frontend draft")


class ApplicationTests(unittest.TestCase):
    def test_ui_module_selection_uses_named_prepare_and_updates_selection(self):
        old_core = _SwappableCore("old-model")
        new_core = _SwappableCore("new-model")
        registry = Registry()
        registry.register("frontend", _PassiveFrontend(), selection="test")
        registry.register("core", old_core, selection="old")
        registry.register("context", _Context(), selection="cloud")
        registry.register("memory", _Memory(), selection="memory")
        registry.register("action", _Action(), selection="native")
        registry.register("tools", ToolRuntime([]), selection=[])
        registry.prepare = Mock(return_value=new_core)
        runtime = ApplicationRuntime(registry)

        try:
            runtime.apply_module_selections({"core": ["new"]})

            registry.prepare.assert_called_once_with("core", "new", {})
            self.assertIs(registry.get("core"), new_core)
            self.assertEqual(registry.selection("core"), "new")
            self.assertTrue(old_core.stopped)
        finally:
            runtime.close()
            registry.close()

    def test_prepared_module_swaps_at_runtime_boundary(self):
        frontend = _PassiveFrontend()
        old_core = _SwappableCore("old-model")
        new_core = _SwappableCore("new-model")
        registry = Registry()
        registry.register("frontend", frontend)
        registry.register("core", old_core)
        registry.register("context", _Context())
        registry.register("memory", _Memory())
        registry.register("action", _Action())
        registry.register("tools", ToolRuntime([]))
        runtime = ApplicationRuntime(registry)

        try:
            runtime.start()
            displaced = runtime.swap_modules({"core": new_core})

            self.assertIs(displaced["core"], old_core)
            self.assertIs(registry.get("core"), new_core)
            self.assertTrue(old_core.stopped)
            self.assertFalse(new_core.stopped)
            self.assertTrue(runtime.is_running)
        finally:
            runtime.close()
            registry.close()

    def test_memory_swap_replaces_its_borrowed_tools(self):
        frontend = _PassiveFrontend()
        old_memory = _SwappableMemory("old")
        new_memory = _SwappableMemory("new")
        tools = ToolRuntime([])
        registry = Registry()
        registry.register("frontend", frontend)
        registry.register("core", _SwappableCore("model"))
        registry.register("context", _Context())
        registry.register("memory", old_memory)
        registry.register("action", _Action())
        registry.register("tools", tools)
        runtime = ApplicationRuntime(registry)

        try:
            runtime.start()
            self.assertEqual(tools.execute("memory_value", {}).content, "old")

            runtime.swap_modules({"memory": new_memory})

            self.assertEqual(tools.execute("memory_value", {}).content, "new")
            self.assertTrue(old_memory.stopped)
        finally:
            runtime.close()
            registry.close()

    def test_continuous_input_worker_moves_to_replacement(self):
        old_input = _SwappableInput()
        new_input = _SwappableInput()
        registry = Registry()
        registry.register("frontend", _PassiveFrontend())
        registry.register("core", _SwappableCore("model"))
        registry.register("context", _Context())
        registry.register("memory", _Memory())
        registry.register("action", _Action())
        registry.register("tools", ToolRuntime([]))
        registry.register("input", old_input)
        runtime = ApplicationRuntime(registry)

        try:
            runtime.start()
            runtime.swap_modules({"input": new_input})

            self.assertTrue(old_input.stopped)
            self.assertIs(registry.get("input"), new_input)
            self.assertTrue(runtime.speech_thread.is_alive())
        finally:
            runtime.close()
            registry.close()

    def test_swap_waits_for_active_turn_and_affects_the_next_generation(self):
        frontend = _Frontend()
        old_core = _SwappableCore("old-model")
        new_core = _SwappableCore("new-model")
        action = _BlockingAction()
        registry = Registry()
        registry.register("frontend", frontend)
        registry.register("core", old_core)
        registry.register("context", _Context())
        registry.register("memory", _Memory())
        registry.register("action", action)
        registry.register("tools", ToolRuntime([]))
        runtime = ApplicationRuntime(registry)
        attachment = threading.Thread(
            target=runtime.attach_frontend,
            args=(frontend,),
        )
        swapped = threading.Event()

        def swap():
            runtime.swap_modules({"core": new_core})
            swapped.set()

        swap_thread = threading.Thread(target=swap)
        try:
            attachment.start()
            self.assertTrue(action.started.wait(timeout=2))
            swap_thread.start()
            self.assertFalse(swapped.wait(timeout=0.05))

            action.release.set()
            attachment.join(timeout=2)
            swap_thread.join(timeout=2)

            self.assertTrue(swapped.is_set())
            self.assertIs(action.core, old_core)
            self.assertIs(registry.get("core"), new_core)
            self.assertTrue(old_core.stopped)
        finally:
            action.release.set()
            runtime.close()
            attachment.join(timeout=2)
            swap_thread.join(timeout=2)
            registry.close()

    def test_frontends_attach_without_owning_runtime_lifetime(self):
        first = _PassiveFrontend()
        second = _PassiveFrontend()
        modules = {
            "frontend": first,
            "core": type("Core", (), {"model": "test-model"})(),
            "context": _Context(),
            "memory": _Memory(),
            "action": _Action(),
            "tools": object(),
        }
        api = type("Api", (), {
            "get": lambda self, name: modules[name],
            "has": lambda self, name: name in modules,
            "assistant_name": "Kayden",
        })()
        runtime = ApplicationRuntime(api)

        try:
            runtime.attach_frontend(first)
            self.assertTrue(runtime.is_running)

            runtime.attach_frontend(second)
            self.assertTrue(runtime.is_running)
        finally:
            runtime.close()

        self.assertFalse(runtime.is_running)
        self.assertIn(("status", "Model: test-model"), first.events)
        self.assertIn(("status", "Model: test-model"), second.events)

    def test_selected_memory_contributes_its_own_actions(self):
        class MemoryActions(ToolInterface):
            def schemas(self):
                return [{
                    "type": "function",
                    "function": {
                        "name": "remember",
                        "parameters": {"type": "object"},
                    },
                }]

            def execute(self, name, arguments):
                return "remembered"

        class ToolMemory(_Memory):
            def actions(self):
                return MemoryActions()

        frontend = _Frontend()
        memory = ToolMemory()
        tools = ToolRuntime([])
        modules = {
            "frontend": frontend,
            "core": type("Core", (), {"model": "test-model"})(),
            "context": _Context(),
            "memory": memory,
            "action": _Action(),
            "tools": tools,
        }
        api = type("Api", (), {
            "get": lambda self, name: modules[name],
            "has": lambda self, name: name in modules,
        })()

        with patch("kayden.runtime.application.load_system_prompt", return_value="system"):
            run_application(api)

        self.assertIn("remember", [
            schema["function"]["name"] for schema in tools.schemas()
        ])
        self.assertEqual(tools.execute("remember", {}).content, "remembered")

    def test_voice_input_and_output_are_connected_to_turns(self):
        frontend = _SpeechFrontend()
        memory = _Memory()
        speech_input = _SpeechInput()
        speech_output = _SpeechOutput()
        modules = {
            "frontend": frontend,
            "core": type("Core", (), {"model": "test-model"})(),
            "context": _Context(),
            "memory": memory,
            "action": _Action(),
            "tools": object(),
            "input": speech_input,
            "output": speech_output,
        }
        api = type("Api", (), {
            "get": lambda self, name: modules[name],
            "has": lambda self, name: name in modules,
        })()

        with patch("kayden.runtime.application.load_system_prompt", return_value="system"):
            run_application(api)

        self.assertIn({"role": "user", "content": "spoken question"}, memory.recorded)
        self.assertEqual(speech_output.events, [
            ("interrupt", None),
            ("send", "hello "),
            ("send", "world"),
            ("flush", None),
        ])

    def test_manual_input_is_transcribed_into_draft_without_auto_submit(self):
        frontend = _ManualFrontend()
        memory = _Memory()
        manual_input = _ManualInput()
        modules = {
            "frontend": frontend,
            "core": type("Core", (), {"model": "test-model"})(),
            "context": _Context(),
            "memory": memory,
            "action": _Action(),
            "tools": object(),
            "input": manual_input,
        }
        api = type("Api", (), {
            "get": lambda self, name: modules[name],
            "has": lambda self, name: name in modules,
        })()

        with patch("kayden.runtime.application.load_system_prompt", return_value="system"):
            run_application(api)

        self.assertEqual(memory.recorded, [])
        self.assertEqual(frontend.capture_events[:4], [
            ("configured", "Mic"),
            ("state", "recording"),
            ("state", "transcribing"),
            ("draft", "drafted speech"),
        ])
        self.assertIn("finish", manual_input.events)

    def test_application_owns_turn_and_persists_final_reply(self):
        frontend = _Frontend()
        context = _Context()
        memory = _Memory()
        action = _Action()
        core = type("Core", (), {"model": "test-model"})()
        tools = object()
        modules = {
            "frontend": frontend,
            "core": core,
            "context": context,
            "memory": memory,
            "action": action,
            "tools": tools,
        }
        api = type("Api", (), {
            "get": lambda self, name: modules[name],
            "has": lambda self, name: name in modules,
        })()

        with patch("kayden.runtime.application.load_system_prompt", return_value="system"):
            run_application(api)

        self.assertEqual(frontend.events, [
            ("status", "Model: test-model"),
            ("user", "question"),
            ("status", "[tool] current_time({})"),
            ("status", "[tool] current_time finished"),
            ("status", "[tool] broken_tool failed: Tool error: bad input"),
            ("assistant", "hello "),
            ("assistant", "world"),
            ("finish", False),
        ])
        self.assertEqual(context.reported_usage, [17])
        self.assertEqual(memory.recorded, [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "hello world"},
        ])
        self.assertEqual(memory.queries, ["question"])
        called_core, turn, called_tools, should_interrupt = action.calls[0]
        self.assertIs(called_core, core)
        self.assertIs(called_tools, tools)
        self.assertFalse(should_interrupt())
        self.assertEqual(turn.messages, [
            {
                "role": "system",
                "content": "system\n\nRelevant memories about the user:\nlikes concise answers",
            },
            {"role": "assistant", "content": "earlier"},
            {"role": "user", "content": "question"},
        ])

    def test_self_counting_context_does_not_receive_provider_usage(self):
        frontend = _Frontend()
        context = _SelfCountingContext()
        modules = {
            "frontend": frontend,
            "core": type("Core", (), {"model": "local-model"})(),
            "context": context,
            "memory": _Memory(),
            "action": _Action(),
            "tools": object(),
        }
        api = type("Api", (), {
            "get": lambda self, name: modules[name],
            "has": lambda self, name: name in modules,
        })()

        with patch("kayden.runtime.application.load_system_prompt", return_value="system"):
            run_application(api)

        self.assertEqual(context.reported_usage, [])

    def test_new_submission_interrupts_then_queues_the_next_turn(self):
        action = _InterruptibleAction()
        frontend = _InterruptingFrontend(action)
        core = _InterruptibleCore()
        context = _Context()
        memory = _Memory()
        modules = {
            "frontend": frontend,
            "core": core,
            "context": context,
            "memory": memory,
            "action": action,
            "tools": object(),
        }
        api = type("Api", (), {
            "get": lambda self, name: modules[name],
            "has": lambda self, name: name in modules,
        })()

        with patch("kayden.runtime.application.load_system_prompt", return_value="system"):
            run_application(api)

        self.assertGreaterEqual(core.interruptions, 1)
        self.assertEqual(action.calls, 2)
        self.assertIn(("finish", True), frontend.events)
        self.assertEqual(frontend.events[-2:], [
            ("assistant", "second reply"),
            ("finish", False),
        ])

    def test_restores_existing_transcript_into_frontend(self):
        frontend = _Frontend()
        memory = type("Memory", (), {
            "recent": lambda self, limit: [
                {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"},
            ],
        })()

        _restore_transcript(frontend, memory)

        self.assertEqual(frontend.events, [
            ("user", "earlier question"),
            ("assistant", "earlier answer"),
            ("finish", False),
        ])

    def test_compacts_with_core_without_exposing_tools(self):
        frontend = _Frontend()
        interrupted = threading.Event()

        class Context:
            keep_recent_messages = 2
            compacted_messages = None

            def needs_compaction(self, view):
                return True

            def compacted(self, view):
                self.compacted_messages = view

        class Memory:
            applied = None

            def view(self):
                return MemoryView("", [{"role": "user", "content": "old detail"}])

            def plan_compaction(self, keep_recent):
                return CompactionPlan(
                    previous_summary="",
                    messages=[{"role": "user", "content": "old detail"}],
                    base_checkpoint=0,
                    through_checkpoint=1,
                )

            def commit_compaction(self, plan, summary):
                self.applied = summary
                return True

        class Core:
            def __init__(self):
                self.tools = "unset"

            def stream(self, messages, tools=None, should_interrupt=None):
                self.tools = tools
                yield CoreTextDelta("- retained old detail")

        context = Context()
        memory = Memory()
        core = Core()

        _compact_context(context, memory, core, frontend, interrupted)

        self.assertIsNone(core.tools)
        self.assertEqual(memory.applied, "- retained old detail")
        self.assertEqual(context.compacted_messages, memory.view())
        self.assertEqual(frontend.events[-1], ("status", "Ready"))

if __name__ == "__main__":
    unittest.main()
