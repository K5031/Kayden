from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kayden.interfaces import (
    ActionInterface,
    ContextPolicyInterface,
    CoreInterface,
    FrontendInterface,
    InputInterface,
    MemoryInterface,
    MemoryView,
    OutputInterface,
    ToolInterface,
)
from kayden.runtime.registry import Registry


class _Frontend(FrontendInterface):
    stopped = False

    def run(self, on_submit, on_interrupt):
        pass

    def user(self, message):
        pass

    def assistant(self, token):
        pass

    def status(self, message):
        pass

    def finish(self, interrupted=False):
        pass

    def stop(self):
        type(self).stopped = True


class _ConfigurableFrontend(_Frontend):
    def __init__(self, mode="full"):
        self.mode = mode


class _Input(InputInterface):
    def get_input(self):
        return ""

    def has_input(self):
        return False


class _Output(OutputInterface):
    def send(self, token):
        pass

    def interrupt(self):
        pass

    def flush(self):
        pass


class _FailingOutput(_Output):
    def __init__(self):
        raise RuntimeError("output failed")


class _CloudCore(CoreInterface):
    runtime_kind = "cloud"
    reports_usage = True

    def stream(self, messages, tools=None, should_interrupt=None):
        return iter(())

    def interrupt(self):
        pass


class _LocalCore(_CloudCore):
    runtime_kind = "local"
    reports_usage = False


class _ReportedContext(ContextPolicyInterface):
    usage_mode = "reported"

    def prepare(self, view):
        return list(view.messages)


class _SelfContext(_ReportedContext):
    usage_mode = "self"


class _Memory(MemoryInterface):
    def append(self, messages):
        pass

    def recall(self, query):
        return ""

    def view(self):
        return MemoryView("", [])


class _Action(ActionInterface):
    def run(self, core, turn, tools, should_interrupt=None):
        return iter(())


class _Tool(ToolInterface):
    def schemas(self):
        return [{
            "type": "function",
            "function": {
                "name": "echo",
                "parameters": {"type": "object"},
            },
        }]

    def execute(self, name, arguments):
        return arguments["text"]


def _config(**changes):
    values = {
        "frontend": "test",
        "core": "cloud_test",
        "context": "reported_test",
        "memory": "test",
        "action": "native_test",
        "tools": ["echo_test"],
    }
    values.update(changes)
    return values


def _modules(**changes):
    values = {
        "kayden.modules.frontend.test": _Frontend,
        "kayden.modules.core.cloud_test": _CloudCore,
        "kayden.modules.context.reported_test": _ReportedContext,
        "kayden.modules.memory.test": _Memory,
        "kayden.modules.action.native_test": _Action,
        "kayden.modules.tools.echo_test": _Tool,
        "kayden.modules.input.one": _Input,
        "kayden.modules.input.two": _Input,
        "kayden.modules.output.test": _Output,
    }
    values.update(changes)
    return values


class RegistryTests(unittest.TestCase):
    def test_tracks_configured_module_names(self):
        modules = _modules()
        with patch(
            "kayden.runtime.registry.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Module=modules[name]),
        ):
            registry = Registry.from_config(_config())

        try:
            self.assertEqual(registry.selection("core"), "cloud_test")
            self.assertEqual(registry.selection("tools"), ["echo_test"])
        finally:
            registry.close()

    def test_prepares_named_module_without_publishing_it(self):
        registry = Registry()
        with patch(
            "kayden.runtime.registry.importlib.import_module",
            return_value=SimpleNamespace(Module=_CloudCore),
        ):
            candidate = registry.prepare("core", "cloud_test")

        self.assertIsInstance(candidate, _CloudCore)
        self.assertFalse(registry.has("core"))

    def test_swap_atomically_replaces_prepared_modules(self):
        registry = Registry()
        old_core = _CloudCore()
        new_core = _CloudCore()
        registry.register("core", old_core)
        registry.register("context", _ReportedContext())
        registry.register("frontend", _Frontend())
        registry.register("memory", _Memory())
        registry.register("action", _Action())
        from kayden.runtime.tools import ToolRuntime
        registry.register("tools", ToolRuntime([]))

        displaced = registry.swap({"core": new_core})

        self.assertIs(displaced["core"], old_core)
        self.assertIs(registry.get("core"), new_core)

    def test_failed_swap_preserves_every_active_module(self):
        registry = Registry()
        core = _CloudCore()
        context = _ReportedContext()
        registry.register("core", core)
        registry.register("context", context)
        registry.register("frontend", _Frontend())
        registry.register("memory", _Memory())
        registry.register("action", _Action())
        from kayden.runtime.tools import ToolRuntime
        registry.register("tools", ToolRuntime([]))

        with self.assertRaisesRegex(ValueError, "incompatible"):
            registry.swap({"context": _SelfContext()})

        self.assertIs(registry.get("core"), core)
        self.assertIs(registry.get("context"), context)

    def test_partial_startup_is_cleaned_up(self):
        _Frontend.stopped = False
        modules = _modules(**{"kayden.modules.output.test": _FailingOutput})

        with patch(
            "kayden.runtime.registry.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Module=modules[name]),
        ):
            with self.assertRaisesRegex(RuntimeError, "output failed"):
                Registry.from_config(_config(output="test"))

        self.assertTrue(_Frontend.stopped)

    def test_incompatible_core_and_context_fail_before_initialization(self):
        modules = _modules(**{"kayden.modules.core.local_test": _LocalCore})

        with patch(
            "kayden.runtime.registry.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Module=modules[name]),
        ):
            with self.assertRaisesRegex(ValueError, "incompatible"):
                Registry.from_config(_config(core="local_test"))

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
        modules = _modules()

        with patch(
            "kayden.runtime.registry.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Module=modules[name]),
        ):
            with self.assertRaisesRegex(ValueError, "only output and tools"):
                Registry.from_config(_config(input=["one", "two"]))

    def test_action_and_tools_are_wired_into_the_runtime(self):
        modules = _modules()

        with patch(
            "kayden.runtime.registry.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Module=modules[name]),
        ):
            registry = Registry.from_config(_config())

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

    def test_legacy_controller_key_is_ignored(self):
        modules = _modules()

        with patch(
            "kayden.runtime.registry.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Module=modules[name]),
        ):
            registry = Registry.from_config({"controller": "cloud", **_config()})

        try:
            self.assertFalse(registry.has("controller"))
        finally:
            registry.close()

    def test_passes_settings_only_to_the_selected_module(self):
        modules = _modules(**{
            "kayden.modules.frontend.test": _ConfigurableFrontend,
        })
        config = _config(settings={
            "frontend": {"test": {"mode": "floaty"}},
            "core": {"unused": {"anything": True}},
        })

        with patch(
            "kayden.runtime.registry.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Module=modules[name]),
        ):
            registry = Registry.from_config(config)

        try:
            self.assertEqual(registry.get("frontend").mode, "floaty")
        finally:
            registry.close()

    def test_rejects_non_table_module_settings(self):
        with self.assertRaisesRegex(TypeError, "settings must be a TOML table"):
            Registry.from_config(_config(settings="wrong"))

    def test_exposes_global_assistant_name_to_the_application(self):
        modules = _modules()
        with patch(
            "kayden.runtime.registry.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Module=modules[name]),
        ):
            registry = Registry.from_config(_config(assistant={"name": "Ahri"}))

        try:
            self.assertEqual(registry.assistant_name, "Ahri")
        finally:
            registry.close()

    def test_rejects_invalid_assistant_name(self):
        with self.assertRaisesRegex(TypeError, "assistant.name"):
            Registry.from_config(_config(assistant={"name": "  "}))

    def test_exposes_exact_permission_rules_to_the_runtime(self):
        modules = _modules()
        with patch(
            "kayden.runtime.registry.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Module=modules[name]),
        ):
            registry = Registry.from_config(_config(permissions={
                "tool.forget_memory": "deny",
            }))

        try:
            self.assertEqual(
                registry.permission_rules,
                {"tool.forget_memory": "deny"},
            )
        finally:
            registry.close()

    def test_rejects_invalid_permission_rules(self):
        with self.assertRaisesRegex(ValueError, "allow, ask, or deny"):
            Registry.from_config(_config(permissions={
                "tool.forget_memory": "sometimes",
            }))


if __name__ == "__main__":
    unittest.main()
