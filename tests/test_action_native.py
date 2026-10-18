import unittest

from kayden.events import CoreTextDelta, CoreToolCall, TextDelta, ToolFinished, ToolStarted, TurnRequest
from kayden.interfaces import ToolInterface
from kayden.modules.action.native import Module as NativeAction
from kayden.runtime.tools import ToolRuntime


class _EchoTool(ToolInterface):
    def __init__(self):
        self.calls = []

    def schemas(self):
        return [{"type": "function", "function": {
            "name": "echo", "description": "Echo text",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        }}]

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return arguments["text"]

    def describe_activity(self, name, arguments, state, result=None):
        return f"Echo {state}: {arguments.get('text', '')}".strip()


class _ScriptedCore:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.messages = []
        self.tools = []
        self.interrupted = False

    def stream(self, messages, tools=None, should_interrupt=None):
        self.messages.append(list(messages))
        self.tools.append(tools)
        yield from next(self.responses)

    def interrupt(self):
        self.interrupted = True


def _turn():
    return TurnRequest([
        {"role": "system", "content": "test"},
        {"role": "user", "content": "say hi"},
    ])


class NativeActionTests(unittest.TestCase):
    def test_executes_tool_and_continues_model(self):
        core = _ScriptedCore(
            [CoreToolCall("call_1", "echo", '{"text": "hello"}')],
            [CoreTextDelta("hello")],
        )
        tool = _EchoTool()

        events = list(NativeAction().run(core, _turn(), ToolRuntime([tool])))

        self.assertEqual("".join(e.text for e in events if isinstance(e, TextDelta)), "hello")
        self.assertEqual(tool.calls, [("echo", {"text": "hello"})])
        self.assertTrue(any(isinstance(e, ToolStarted) for e in events))
        self.assertTrue(any(isinstance(e, ToolFinished) for e in events))
        started = next(e for e in events if isinstance(e, ToolStarted))
        finished = next(e for e in events if isinstance(e, ToolFinished))
        self.assertEqual(started.description, "Echo running: hello…")
        self.assertEqual(finished.description, "Echo finished: hello")
        self.assertEqual(core.messages[1][-1], {
            "role": "tool", "tool_call_id": "call_1", "content": "hello",
        })

    def test_bad_arguments_are_returned_to_model_as_tool_error(self):
        core = _ScriptedCore(
            [CoreToolCall("call_1", "echo", "not json")],
            [CoreTextDelta("Could not run that.")],
        )
        tool = _EchoTool()

        events = list(NativeAction().run(core, _turn(), ToolRuntime([tool])))

        self.assertEqual(tool.calls, [])
        self.assertTrue(any(isinstance(e, ToolFinished) and e.result.is_error for e in events))
        self.assertIn("Tool error:", core.messages[1][-1]["content"])

    def test_multiple_tool_calls_are_executed_before_next_model_request(self):
        core = _ScriptedCore(
            [CoreToolCall("call_1", "echo", '{"text": "one"}'),
             CoreToolCall("call_2", "echo", '{"text": "two"}')],
            [CoreTextDelta("done")],
        )
        tool = _EchoTool()

        events = list(NativeAction().run(core, _turn(), ToolRuntime([tool])))

        self.assertEqual(tool.calls, [("echo", {"text": "one"}), ("echo", {"text": "two"})])
        self.assertEqual([e.call_id for e in events if isinstance(e, ToolStarted)], ["call_1", "call_2"])
        self.assertEqual(core.messages[1][-2:], [
            {"role": "tool", "tool_call_id": "call_1", "content": "one"},
            {"role": "tool", "tool_call_id": "call_2", "content": "two"},
        ])

    def test_tool_round_limit_stops_without_executing_an_extra_call(self):
        core = _ScriptedCore(
            [CoreToolCall("call_1", "echo", '{"text": "one"}')],
            [CoreToolCall("call_2", "echo", '{"text": "two"}')],
        )
        tool = _EchoTool()

        events = list(NativeAction(max_rounds=1).run(core, _turn(), ToolRuntime([tool])))

        self.assertEqual(tool.calls, [("echo", {"text": "one"})])
        self.assertEqual(len(core.messages), 2)
        self.assertEqual("".join(e.text for e in events if isinstance(e, TextDelta)), " [tool limit reached]")

    def test_interrupt_before_request_stops_the_core(self):
        core = _ScriptedCore([CoreTextDelta("should not appear")])

        events = list(NativeAction().run(core, _turn(), ToolRuntime([]), should_interrupt=lambda: True))

        self.assertEqual(events, [])
        self.assertTrue(core.interrupted)
        self.assertEqual(core.messages, [])

    def test_empty_tool_runtime_does_not_advertise_tools(self):
        core = _ScriptedCore([CoreTextDelta("ok")])

        events = list(NativeAction().run(core, _turn(), ToolRuntime([])))

        self.assertEqual("".join(e.text for e in events if isinstance(e, TextDelta)), "ok")
        self.assertEqual(core.tools, [None])


if __name__ == "__main__":
    unittest.main()
