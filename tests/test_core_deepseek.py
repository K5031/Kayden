import json
import logging
import unittest

import requests

from kayden.events import CoreReasoningDelta, CoreTextDelta, CoreToolCall, CoreUsage
from kayden.modules.core.deepseek import Module as DeepSeekCore


def _event(delta=None, usage=None):
    chunk = {"choices": [{"delta": delta}]} if delta is not None else {"choices": []}
    if usage is not None:
        chunk["usage"] = usage
    return f"data: {json.dumps(chunk)}".encode()


class _Response:
    def __init__(self, *events, status_error=None):
        self.events = events
        self.status_error = status_error
        self.text = "error details"
        self.closed = False

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def iter_lines(self):
        return iter(self.events)

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


def _core(session):
    core = DeepSeekCore.__new__(DeepSeekCore)
    core.log = logging.getLogger("test-deepseek")
    core.model = "test-model"
    core.thinking = "disabled"
    core.api_key = "test-key"
    core._session = session
    core._interrupted = False
    core.last_usage = None
    return core


class DeepSeekCoreTests(unittest.TestCase):
    def test_streams_reasoning_text_usage_and_fragmented_tool_call(self):
        response = _Response(
            _event({"reasoning_content": "thinking"}),
            _event({"content": "hello"}),
            _event({"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "echo", "arguments": '{"text":'}}]}),
            _event({"tool_calls": [{"index": 0, "function": {"arguments": '"hi"}'}}]}),
            _event(usage={"prompt_tokens": 12}),
            b"data: [DONE]",
        )
        session = _Session(response)
        tools = [{"type": "function", "function": {"name": "echo"}}]

        events = list(_core(session).stream([{"role": "user", "content": "hi"}], tools=tools))

        self.assertEqual(events, [
            CoreReasoningDelta("thinking"), CoreTextDelta("hello"),
            CoreUsage({"prompt_tokens": 12}), CoreToolCall("call_1", "echo", '{"text":"hi"}'),
        ])
        url, request = session.calls[0]
        self.assertEqual(url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request["json"]["tools"], tools)
        self.assertEqual(request["json"]["thinking"], {"type": "disabled"})
        self.assertTrue(response.closed)

    def test_omits_tools_when_none_are_available(self):
        session = _Session(_Response(_event({"content": "ok"}), b"data: [DONE]"))
        self.assertEqual(list(_core(session).stream([], tools=[])), [CoreTextDelta("ok")])
        self.assertNotIn("tools", session.calls[0][1]["json"])

    def test_malformed_stream_events_are_ignored(self):
        response = _Response(b"data: {not-json", b"data: \xff", _event({"content": "still works"}))
        self.assertEqual(list(_core(_Session(response)).stream([])), [CoreTextDelta("still works")])
        self.assertTrue(response.closed)

    def test_reasoning_only_stream_can_be_interrupted(self):
        response = _Response(_event({"reasoning_content": "thinking"}), _event({"content": "too late"}))
        checks = iter([False, False, True])
        events = list(_core(_Session(response)).stream([], should_interrupt=lambda: next(checks, True)))
        self.assertFalse(any(isinstance(event, CoreTextDelta) for event in events))
        self.assertTrue(response.closed)

    def test_request_failure_becomes_visible_text(self):
        session = _Session(error=requests.ConnectionError("offline"))
        self.assertEqual(list(_core(session).stream([])), [CoreTextDelta("[request failed — check your network and logs]")])

    def test_incomplete_tool_call_is_rejected(self):
        response = _Response(_event({"tool_calls": [{"index": 0, "id": "call_1", "function": {"arguments": "{}"}}]}))
        self.assertEqual(list(_core(_Session(response)).stream([])), [CoreTextDelta(" [invalid tool request]")])

    def test_stop_closes_session(self):
        session = _Session()
        _core(session).stop()
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
