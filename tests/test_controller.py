import threading
import time
import unittest
from unittest.mock import patch

from events import (
    CoreTextDelta,
    TextDelta,
    ToolFinished,
    ToolResult,
    ToolStarted,
    UsageReported,
)
from modules.controller._frontend import _compact_context, _restore_transcript
from modules.controller.cloud import Module as CloudController
from interfaces import CompactionPlan, MemoryView, ToolInterface
from tool_runtime import ToolRuntime


class _Frontend:
    def __init__(self):
        self.events = []
        self.done = threading.Event()

    def run(self, on_submit, on_interrupt):
        on_submit("question")
        if not self.done.wait(timeout=2):
            raise AssertionError("controller did not finish the turn")

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


class ControllerTests(unittest.TestCase):
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

        with patch("modules.controller._frontend.load_system_prompt", return_value="system"):
            CloudController().run(api)

        self.assertIn("remember", [
            schema["function"]["name"] for schema in tools.schemas()
        ])
        self.assertEqual(tools.execute("remember", {}).content, "remembered")

    def test_cloud_controller_owns_turn_and_persists_final_reply(self):
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

        with patch("modules.controller._frontend.load_system_prompt", return_value="system"):
            CloudController().run(api)

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

        with patch("modules.controller._frontend.load_system_prompt", return_value="system"):
            CloudController().run(api)

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
