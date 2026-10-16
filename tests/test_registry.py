from types import SimpleNamespace
import unittest
from unittest.mock import patch

from interfaces import (
    ActionInterface,
    ContextInterface,
    ControllerInterface,
    CoreInterface,
    InputInterface,
    OutputInterface,
    ToolInterface,
)
from registry import Registry


class _Controller(ControllerInterface):
    required_modules = ["input", "output"]

    def run(self, api):
        pass


class _Input(InputInterface):
    stopped = False

    def get_input(self):
        return ""

    def has_input(self):
        return False

    def stop(self):
        type(self).stopped = True


class _FailingOutput(OutputInterface):
    def __init__(self):
        raise RuntimeError("output failed")

    def send(self, token):
        pass

    def interrupt(self):
        pass

    def flush(self):
        pass


class _CloudController(ControllerInterface):
    required_modules = ["core", "context"]
    required_features = {
        "core": {"runtime_kind": "cloud"},
        "context": {"usage_mode": "reported"},
    }

    def run(self, api):
        pass


class _LocalCore(CoreInterface):
    runtime_kind = "local"

    def stream(self, messages, tools=None, should_interrupt=None):
        return iter(())

    def interrupt(self):
        pass


class _ReportedContext(ContextInterface):
    usage_mode = "reported"

    def add(self, role, content):
        pass

    def get(self):
        return []

    def clear(self):
        pass


class _ActionController(ControllerInterface):
    required_modules = ["action", "tools"]

    def run(self, api):
        pass


class _Action(ActionInterface):
    def run(self, core, turn, tools, should_interrupt=None):
        return iter(())


class _Tool(ToolInterface):
    def schemas(self):
        return [{
            "type": "function",
            "function": {"name": "echo", "parameters": {"type": "object"}},
        }]

    def execute(self, name, arguments):
        return arguments["text"]


class RegistryTests(unittest.TestCase):
    def test_partial_startup_is_cleaned_up(self):
        _Input.stopped = False
        modules = {
            "modules.controller.test": _Controller,
            "modules.input.test": _Input,
            "modules.output.test": _FailingOutput,
        }

        with patch("registry.importlib.import_module", side_effect=lambda name: SimpleNamespace(Module=modules[name])):
            with self.assertRaisesRegex(RuntimeError, "output failed"):
                Registry.from_config({
                    "controller": "test",
                    "input": "test",
                    "output": "test",
                })

        self.assertTrue(_Input.stopped)

    def test_incompatible_modules_fail_before_initialization(self):
        modules = {
            "modules.controller.cloud_test": _CloudController,
            "modules.core.local_test": _LocalCore,
            "modules.context.reported_test": _ReportedContext,
        }

        with patch("registry.importlib.import_module", side_effect=lambda name: SimpleNamespace(Module=modules[name])):
            with self.assertRaisesRegex(ValueError, "incompatible"):
                Registry.from_config({
                    "controller": "cloud_test",
                    "core": "local_test",
                    "context": "reported_test",
                })

    def test_close_runs_in_reverse_order_and_is_idempotent(self):
        stopped = []

        class Stoppable:
            def __init__(self, name):
                self.name = name

            def stop(self):
                stopped.append(self.name)

        registry = Registry()
        registry.register("first", Stoppable("first"))
        registry.register("second", Stoppable("second"))
        registry.close()
        registry.close()

        self.assertEqual(stopped, ["second", "first"])

    def test_only_output_and_tools_accept_multiple_modules(self):
        modules = {
            "modules.controller.test": _Controller,
            "modules.input.one": _Input,
            "modules.input.two": _Input,
            "modules.output.test": _FailingOutput,
        }

        with patch("registry.importlib.import_module", side_effect=lambda name: SimpleNamespace(Module=modules[name])):
            with self.assertRaisesRegex(ValueError, "only output and tools"):
                Registry.from_config({
                    "controller": "test",
                    "input": ["one", "two"],
                    "output": "test",
                })

    def test_action_and_tools_are_wired_into_the_runtime(self):
        modules = {
            "modules.controller.actions": _ActionController,
            "modules.action.native_test": _Action,
            "modules.tools.echo_test": _Tool,
        }

        with patch("registry.importlib.import_module", side_effect=lambda name: SimpleNamespace(Module=modules[name])):
            registry = Registry.from_config({
                "controller": "actions",
                "action": "native_test",
                "tools": ["echo_test"],
            })

        try:
            self.assertIsInstance(registry.get("action"), _Action)
            self.assertEqual(
                registry.get("tools").schemas()[0]["function"]["name"],
                "echo",
            )
            self.assertEqual(
                registry.get("tools").execute("echo", {"text": "hello"}).content,
                "hello",
            )
        finally:
            registry.close()


if __name__ == "__main__":
    unittest.main()
