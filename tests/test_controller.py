import contextlib
import io
import unittest
from unittest.mock import patch

from events import TextDelta, UsageReported
from modules.controller.cloud import Module as CloudController


class _Input:
    def __init__(self):
        self.reads = 0

    def get_input(self):
        self.reads += 1
        if self.reads == 1:
            return "question"
        raise EOFError

    def has_input(self):
        return False


class _Output:
    def __init__(self):
        self.sent = []
        self.interruptions = 0
        self.flushes = 0

    def send(self, text):
        self.sent.append(text)

    def interrupt(self):
        self.interruptions += 1

    def flush(self):
        self.flushes += 1


class _Context:
    def __init__(self):
        self.messages = [{"role": "assistant", "content": "earlier"}]
        self.added = []
        self.reported_usage = []

    def get(self):
        return list(self.messages)

    def add(self, role, content):
        self.added.append((role, content))

    def report_usage(self, usage):
        self.reported_usage.append(usage)


class _Memory:
    def __init__(self):
        self.queries = []
        self.stored = []

    def retrieve(self, query):
        self.queries.append(query)
        return "likes concise answers"

    def store(self, messages):
        self.stored.append(messages)


class _Action:
    def __init__(self):
        self.calls = []

    def run(self, core, turn, tools, should_interrupt=None):
        self.calls.append((core, turn, tools, should_interrupt))
        yield TextDelta("hello ")
        yield UsageReported({"prompt_tokens": 17})
        yield TextDelta("world")


class ControllerTests(unittest.TestCase):
    def test_cloud_controller_owns_turn_and_persists_final_reply(self):
        inp = _Input()
        output = _Output()
        context = _Context()
        memory = _Memory()
        action = _Action()
        core = type("Core", (), {"model": "test-model"})()
        tools = object()
        modules = {
            "input": inp,
            "output": output,
            "core": core,
            "context": context,
            "memory": memory,
            "action": action,
            "tools": tools,
        }
        api = type("Api", (), {"get": lambda self, name: modules[name]})()

        with patch("modules.controller.cloud.load_system_prompt", return_value="system"):
            with contextlib.redirect_stdout(io.StringIO()):
                CloudController().run(api)

        self.assertEqual(output.sent, ["hello ", "world"])
        self.assertEqual(output.flushes, 1)
        self.assertEqual(context.reported_usage, [17])
        self.assertEqual(context.added, [
            ("user", "question"),
            ("assistant", "hello world"),
        ])
        self.assertEqual(memory.queries, ["question"])
        self.assertEqual(memory.stored, [[
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "hello world"},
        ]])

        called_core, turn, called_tools, should_interrupt = action.calls[0]
        self.assertIs(called_core, core)
        self.assertIs(called_tools, tools)
        self.assertIs(should_interrupt.__self__, inp)
        self.assertIs(should_interrupt.__func__, inp.has_input.__func__)
        self.assertEqual(turn.messages, [
            {
                "role": "system",
                "content": "system\n\nRelevant memories about the user:\nlikes concise answers",
            },
            {"role": "assistant", "content": "earlier"},
            {"role": "user", "content": "question"},
        ])


if __name__ == "__main__":
    unittest.main()
