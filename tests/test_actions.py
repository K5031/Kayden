import json
import logging
import unittest

from events import (
    CoreTextDelta,
    CoreToolCall,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnRequest,
)
from interfaces import ToolInterface
from modules.action.native import Module as NativeAction
from modules.core.deepseek import Module as DeepSeekCore
from modules.tools.clock import Module as ClockTool
from tool_runtime import ToolRuntime


def _event(delta=None, usage=None):
    chunk = {"choices": [{"delta": delta}]} if delta is not None else {"choices": []}
    if usage is not None:
        chunk["usage"] = usage
    return f"data: {json.dumps(chunk)}".encode()


class _Response:
    def __init__(self, *events):
        self.events = events
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return iter(self.events)

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.payloads = []

    def post(self, url, headers, json, stream, timeout):
        self.payloads.append(json)
        return next(self.responses)


class _EchoTool(ToolInterface):
    def __init__(self):
        self.calls = []

    def schemas(self):
        return [{"type": "function", "function": {
            "name": "echo",
            "description": "Echo text",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        }}]

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return arguments["text"]


class _ScriptedCore:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.messages = []
        self.interrupted = False

    def stream(self, messages, tools=None, should_interrupt=None):
        self.messages.append(list(messages))
        yield from next(self.responses)

    def interrupt(self):
        self.interrupted = True


def _core(session):
    core = DeepSeekCore.__new__(DeepSeekCore)
    core.model = "test-model"
    core.thinking = "disabled"
    core.api_key = "test-key"
    core._session = session
    core._interrupted = False
    core.last_usage = None
    core.log = logging.getLogger("test")
    return core


def _turn():
    return TurnRequest([
        {"role": "system", "content": "test"},
        {"role": "user", "content": "say hi"},
    ])


class ActionTests(unittest.TestCase):
    def test_native_strategy_executes_tool_and_continues_model(self):
        first = _Response(
            _event({"tool_calls": [{
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text":'},
            }]}),
            _event({"tool_calls": [{
                "index": 0,
                "function": {"arguments": '"hello"}'},
            }]}),
        )
        second = _Response(
            _event({"content": "hello"}),
            _event(usage={"prompt_tokens": 23}),
        )
        session = _Session([first, second])
        tool = _EchoTool()

        events = list(NativeAction().run(
            _core(session), _turn(), ToolRuntime([tool])
        ))

        self.assertEqual(
            "".join(event.text for event in events if isinstance(event, TextDelta)),
            "hello",
        )
        self.assertEqual(tool.calls, [("echo", {"text": "hello"})])
        self.assertTrue(any(isinstance(event, ToolStarted) for event in events))
        self.assertTrue(any(isinstance(event, ToolFinished) for event in events))
        self.assertEqual(session.payloads[1]["messages"][-1], {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "hello",
        })
        self.assertTrue(first.closed and second.closed)

    def test_bad_arguments_are_returned_to_model_as_tool_error(self):
        first = _Response(_event({"tool_calls": [{
            "index": 0,
            "id": "call_1",
            "function": {"name": "echo", "arguments": "not json"},
        }]}))
        second = _Response(_event({"content": "Could not run that."}))
        session = _Session([first, second])
        tool = _EchoTool()

        events = list(NativeAction().run(
            _core(session), _turn(), ToolRuntime([tool])
        ))

        self.assertEqual(tool.calls, [])
        self.assertTrue(any(
            isinstance(event, ToolFinished) and event.result.is_error
            for event in events
        ))
        self.assertIn("Tool error:", session.payloads[1]["messages"][-1]["content"])

    def test_multiple_tool_calls_are_executed_before_next_model_request(self):
        core = _ScriptedCore(
            [
                CoreToolCall("call_1", "echo", '{"text": "one"}'),
                CoreToolCall("call_2", "echo", '{"text": "two"}'),
            ],
            [CoreTextDelta("done")],
        )
        tool = _EchoTool()

        events = list(NativeAction().run(core, _turn(), ToolRuntime([tool])))

        self.assertEqual(tool.calls, [
            ("echo", {"text": "one"}),
            ("echo", {"text": "two"}),
        ])
        self.assertEqual(
            [event.call_id for event in events if isinstance(event, ToolStarted)],
            ["call_1", "call_2"],
        )
        self.assertEqual(
            core.messages[1][-2:],
            [
                {"role": "tool", "tool_call_id": "call_1", "content": "one"},
                {"role": "tool", "tool_call_id": "call_2", "content": "two"},
            ],
        )

    def test_tool_round_limit_stops_without_executing_an_extra_call(self):
        core = _ScriptedCore(
            [CoreToolCall("call_1", "echo", '{"text": "one"}')],
            [CoreToolCall("call_2", "echo", '{"text": "two"}')],
        )
        tool = _EchoTool()

        events = list(NativeAction(max_rounds=1).run(
            core, _turn(), ToolRuntime([tool])
        ))

        self.assertEqual(tool.calls, [("echo", {"text": "one"})])
        self.assertEqual(len(core.messages), 2)
        self.assertEqual(
            "".join(event.text for event in events if isinstance(event, TextDelta)),
            " [tool limit reached]",
        )

    def test_interrupt_before_request_stops_the_core(self):
        core = _ScriptedCore([CoreTextDelta("should not appear")])

        events = list(NativeAction().run(
            core,
            _turn(),
            ToolRuntime([]),
            should_interrupt=lambda: True,
        ))

        self.assertEqual(events, [])
        self.assertTrue(core.interrupted)
        self.assertEqual(core.messages, [])

    def test_empty_tool_runtime_does_not_advertise_tools(self):
        session = _Session([_Response(_event({"content": "ok"}))])

        events = list(NativeAction().run(
            _core(session), _turn(), ToolRuntime([])
        ))

        self.assertEqual(
            "".join(event.text for event in events if isinstance(event, TextDelta)),
            "ok",
        )
        self.assertNotIn("tools", session.payloads[0])

    def test_malformed_stream_events_are_ignored(self):
        response = _Response(
            b"data: {not-json",
            b"data: \xff",
            _event({"content": "still works"}),
        )

        events = list(_core(_Session([response])).stream(_turn().messages))

        self.assertEqual(
            "".join(event.text for event in events if isinstance(event, CoreTextDelta)),
            "still works",
        )
        self.assertTrue(response.closed)

    def test_reasoning_only_stream_can_be_interrupted(self):
        response = _Response(
            _event({"reasoning_content": "thinking"}),
            _event({"content": "too late"}),
        )
        checks = iter([False, False, True])

        events = list(_core(_Session([response])).stream(
            _turn().messages,
            should_interrupt=lambda: next(checks, True),
        ))

        self.assertFalse(any(isinstance(event, CoreTextDelta) for event in events))
        self.assertTrue(response.closed)

    def test_clock_rejects_unknown_tool_and_arguments(self):
        clock = ClockTool()
        with self.assertRaises(ValueError):
            clock.execute("other", {})
        with self.assertRaises(ValueError):
            clock.execute("current_time", {"unexpected": True})


if __name__ == "__main__":
    unittest.main()
