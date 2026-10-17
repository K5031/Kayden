import json
import logging
import unittest

import requests

from events import CoreTextDelta, CoreToolCall, CoreUsage
from modules.core.openai import Module as OpenAICore


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
    core = OpenAICore.__new__(OpenAICore)
    core.log = logging.getLogger("test-openai")
    core.model = "test-model"
    core.api_key = "test-key"
    core._session = session
    core._interrupted = False
    core.last_usage = None
    return core


class OpenAICoreTests(unittest.TestCase):
    def test_streams_text_usage_and_fragmented_tool_call(self):
        response = _Response(
            _event({"content": "hello"}),
            _event({"tool_calls": [{
                "index": 0,
                "id": "call_1",
                "function": {"name": "echo", "arguments": '{"text":'},
            }]}),
            _event({"tool_calls": [{
                "index": 0,
                "function": {"arguments": '"hi"}'},
            }]}),
            _event(usage={"prompt_tokens": 12, "completion_tokens": 3}),
            b"data: [DONE]",
        )
        session = _Session(response)
        tools = [{"type": "function", "function": {"name": "echo"}}]

        events = list(_core(session).stream(
            [{"role": "user", "content": "hi"}], tools=tools
        ))

        self.assertEqual(events, [
            CoreTextDelta("hello"),
            CoreUsage({"prompt_tokens": 12, "completion_tokens": 3}),
            CoreToolCall("call_1", "echo", '{"text":"hi"}'),
        ])
        url, request = session.calls[0]
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(request["json"]["tools"], tools)
        self.assertEqual(request["json"]["max_completion_tokens"], 512)
        self.assertEqual(request["json"]["reasoning_effort"], "none")
        self.assertTrue(response.closed)

    def test_omits_tools_when_none_are_available(self):
        response = _Response(_event({"content": "ok"}), b"data: [DONE]")
        session = _Session(response)

        events = list(_core(session).stream(
            [{"role": "user", "content": "hi"}], tools=[]
        ))

        self.assertEqual(events, [CoreTextDelta("ok")])
        self.assertNotIn("tools", session.calls[0][1]["json"])

    def test_request_failure_becomes_visible_text(self):
        session = _Session(error=requests.ConnectionError("offline"))

        events = list(_core(session).stream([]))

        self.assertEqual(
            events,
            [CoreTextDelta("[request failed — check your network and logs]")],
        )

    def test_incomplete_tool_call_is_rejected(self):
        response = _Response(
            _event({"tool_calls": [{
                "index": 0,
                "id": "call_1",
                "function": {"arguments": "{}"},
            }]}),
            b"data: [DONE]",
        )

        events = list(_core(_Session(response)).stream([]))

        self.assertEqual(events, [CoreTextDelta(" [invalid tool request]")])
        self.assertTrue(response.closed)

    def test_stop_closes_session(self):
        session = _Session()
        core = _core(session)

        core.stop()

        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
