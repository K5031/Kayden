from pathlib import Path
import struct
import unittest
from unittest.mock import Mock, patch

from kayden.frontend_api import (
    ControlsConfigured,
    FrontendAPI,
    IdentityConfigured,
    SubmitMessage,
)
from kayden.interfaces import FrontendInterface
from kayden.events import ToolResult
from kayden.modules.frontend.qt import Module as QtFrontend
from kayden.modules.frontend.qt.markdown import render_markdown


class QtFrontendTests(unittest.TestCase):
    def test_is_a_frontend_with_configurable_window_size(self):
        frontend = QtFrontend(width=900, height=640)

        self.assertIsInstance(frontend, FrontendInterface)
        self.assertEqual((frontend.width, frontend.height), (900, 640))

    def test_rejects_unusable_window_dimensions(self):
        with self.assertRaisesRegex(ValueError, "width"):
            QtFrontend(width=400)
        with self.assertRaisesRegex(ValueError, "height"):
            QtFrontend(height=300)

    def test_consumes_typed_frontend_api_and_sends_typed_commands(self):
        commands = []
        api = FrontendAPI(commands.append)
        api._publish(IdentityConfigured("Ahri"))
        api._publish(ControlsConfigured([]))
        frontend = QtFrontend()

        with patch(
            "kayden.modules.frontend.qt.backend.run_qt",
            side_effect=lambda active: active._submit("hello"),
        ):
            frontend.run(api)

        self.assertEqual(frontend.assistant_name, "Ahri")
        self.assertEqual(commands, [SubmitMessage("hello")])

    def test_queues_streaming_and_capture_events_before_event_loop_starts(self):
        frontend = QtFrontend()
        frontend.user("hello")
        frontend.assistant("Hi")
        frontend.status("[tool] current_time({})")
        frontend.tool_started(
            "call_1",
            "search_web",
            {"query": "latest news"},
            "Searching the web for “latest news”…",
        )
        frontend.tool_finished(
            "call_1",
            "search_web",
            ToolResult("results"),
            "Searched the web for “latest news”",
        )
        frontend.finish()
        frontend.capture_state("recording")
        frontend.draft("voice text")

        self.assertEqual(list(frontend._drain_events()), [
            ("user", "hello"),
            ("assistant", "Hi"),
            ("status", "[tool] current_time({})"),
            (
                "tool_group",
                (
                    1,
                    "Searching the web for “latest news”…",
                    "running",
                    "◌  Searching the web for “latest news”…",
                    1,
                ),
            ),
            (
                "tool_group",
                (
                    1,
                    "Searched the web for “latest news”",
                    "finished",
                    "✓  Searched the web for “latest news”",
                    1,
                ),
            ),
            ("finish", False),
            ("capture_state", "recording"),
            ("draft", "voice text"),
        ])

    def test_routes_user_commands_to_runtime_callbacks(self):
        frontend = QtFrontend()
        submit = Mock()
        interrupt = Mock()
        frontend._on_submit = submit
        frontend._on_interrupt = interrupt

        frontend._submit("  hello  ")
        frontend._interrupt()

        submit.assert_called_once_with("hello")
        interrupt.assert_called_once_with()

    def test_tool_failures_are_human_readable_and_keep_the_call_id(self):
        frontend = QtFrontend()

        frontend.tool_started(
            "call_fetch",
            "fetch_webpage",
            {"url": "https://example.com/article"},
            "Reading example.com…",
        )
        frontend.tool_finished(
            "call_fetch",
            "fetch_webpage",
            ToolResult("Tool error: HTTP 403 from example.com", is_error=True),
            "Couldn’t read example.com — HTTP 403 from example.com",
        )

        self.assertEqual(list(frontend._drain_events()), [
            (
                "tool_group",
                (
                    1,
                    "Reading example.com…",
                    "running",
                    "◌  Reading example.com…",
                    1,
                ),
            ),
            (
                "tool_group",
                (
                    1,
                    "Couldn’t read example.com — HTTP 403 from example.com",
                    "failed",
                    "!  Couldn’t read example.com — HTTP 403 from example.com",
                    1,
                ),
            ),
        ])

    def test_multiple_tools_update_one_expandable_group(self):
        frontend = QtFrontend()
        frontend.user("find and read it")
        list(frontend._drain_events())

        frontend.tool_started(
            "search",
            "search_web",
            {"query": "news"},
            "Searching the web for “news”…",
        )
        frontend.tool_finished(
            "search",
            "search_web",
            ToolResult("results"),
            "Searched the web for “news”",
        )
        frontend.tool_started(
            "fetch",
            "fetch_webpage",
            {"url": "https://example.com"},
            "Reading example.com…",
        )

        groups = list(frontend._drain_events())
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(kind == "tool_group" for kind, _ in groups))
        self.assertEqual(groups[-1][1], (
            1,
            "Reading example.com…",
            "running",
            "✓  Searched the web for “news”\n◌  Reading example.com…",
            2,
        ))

    def test_assistant_text_closes_group_before_a_later_tool(self):
        frontend = QtFrontend()
        frontend.user("do two stages")
        list(frontend._drain_events())

        frontend.tool_started(
            "first", "current_time", {}, "Checking the time…"
        )
        frontend.tool_finished(
            "first", "current_time", ToolResult("12:00"), "Checked the time"
        )
        frontend.assistant("Now I’ll search.")
        frontend.tool_started(
            "second",
            "search_web",
            {"query": "news"},
            "Searching the web for “news”…",
        )

        events = list(frontend._drain_events())
        self.assertEqual(events[-2], ("assistant", "Now I’ll search."))
        self.assertEqual(events[-1], (
            "tool_group",
            (
                2,
                "Searching the web for “news”…",
                "running",
                "◌  Searching the web for “news”…",
                1,
            ),
        ))

    def test_exposes_identity_models_and_model_selection(self):
        frontend = QtFrontend()
        selected = Mock()
        frontend.configure_identity("Ahri")

        supported = frontend.configure_models([
            {"id": "gpt-fast", "name": "GPT Fast"},
            {"id": "gpt-deep", "name": "GPT Deep"},
        ], "gpt-fast", selected)
        frontend._select_model("gpt-deep")

        self.assertTrue(supported)
        self.assertEqual(frontend.assistant_name, "Ahri")
        self.assertEqual(frontend._selected_model, "gpt-deep")
        selected.assert_called_once_with("gpt-deep")
        self.assertEqual(list(frontend._drain_events()), [
            ("model", ("gpt-deep", "GPT Deep")),
        ])

    def test_rejects_unadvertised_model(self):
        frontend = QtFrontend()
        frontend.configure_models(
            [{"id": "gpt-fast", "name": "GPT Fast"}],
            "gpt-fast",
            Mock(),
        )

        with self.assertRaisesRegex(ValueError, "unavailable"):
            frontend._select_model("gpt-hidden")

    def test_exposes_generic_module_catalog_and_applies_in_background(self):
        frontend = QtFrontend()
        applied = Mock()
        completed = __import__("threading").Event()

        def apply(selections):
            applied(selections)
            completed.set()

        supported = frontend.configure_modules([
            {
                "id": "memory",
                "name": "Memory",
                "multiple": False,
                "optional": False,
                "options": [
                    {"id": "continuity", "name": "Continuity"},
                    {"id": "remis", "name": "Remis"},
                ],
                "selected": ["continuity"],
            },
        ], apply)
        list(frontend._drain_events())

        frontend._apply_modules('{"memory":["remis"]}')

        self.assertTrue(completed.wait(timeout=2))
        applied.assert_called_once_with({"memory": ["remis"]})
        events = list(frontend._drain_events())
        self.assertEqual(events[0], ("modules_busy", (True, "Applying modules…")))
        self.assertEqual(events[-1], ("modules_busy", (False, "")))
        self.assertTrue(supported)

    def test_rejects_unadvertised_module_before_applying(self):
        frontend = QtFrontend()
        frontend.configure_modules([
            {
                "id": "core",
                "name": "Core",
                "multiple": False,
                "optional": False,
                "options": [{"id": "chatgpt", "name": "Chatgpt"}],
                "selected": ["chatgpt"],
            },
        ], Mock())

        with self.assertRaisesRegex(ValueError, "unavailable"):
            frontend._apply_modules('{"core":["hidden"]}')

    def test_routes_manual_capture_controls(self):
        frontend = QtFrontend()
        start, finish, cancel = Mock(), Mock(), Mock()

        self.assertTrue(frontend.configure_capture(start, finish, cancel, "Mic"))
        frontend._capture("start")
        frontend._capture("finish")
        frontend._capture("cancel")

        self.assertEqual(frontend._capture_label, "Mic")
        start.assert_called_once_with()
        finish.assert_called_once_with()
        cancel.assert_called_once_with()

        frontend.clear_capture()
        self.assertIsNone(frontend._on_capture_start)
        self.assertEqual(
            list(frontend._drain_events())[-1],
            ("capture_config", (False, "Capture")),
        )

    def test_module_owns_its_qml_and_metadata(self):
        module = (
            Path(__file__).resolve().parents[1]
            / "src/kayden/modules/frontend/qt"
        )

        self.assertTrue((module / "view.qml").is_file())
        self.assertIn('name = "qt"', (module / "module.toml").read_text())
        qml = (module / "view.qml").read_text()
        self.assertIn("backend.selectModel", qml)
        self.assertIn("backend.applyModules", qml)
        self.assertIn("backend.invokeControl", qml)
        self.assertIn("onControlsReceived", qml)
        self.assertIn("onModulesReceived", qml)
        self.assertIn("backend.startCapture", qml)
        self.assertIn("backend.interrupt", qml)
        self.assertIn("onToolGroupReceived", qml)
        self.assertIn("root.updateToolGroup", qml)
        self.assertIn("activeAssistant = -1", qml)
        self.assertIn("activeToolGroup", qml)
        self.assertIn("activeToolGroupId", qml)
        self.assertIn("toolExpanded", qml)
        self.assertIn("toolDetails", qml)
        self.assertIn("maximumLineCount: 1", qml)
        self.assertIn("property real feedWidth", qml)
        self.assertIn("root.feedWidth", qml)
        self.assertIn("width - pageMargin * 2", qml)
        self.assertIn("Math.min(80, Math.max(20, width * 0.052))", qml)
        self.assertIn("property real assistantWidth: Math.min(1180, feedWidth)", qml)
        self.assertIn('x: kind === "user" ? parent.width - width : 0', qml)
        self.assertIn('modelOption.highlighted ? "#404040"', qml)

        backend = (module / "backend.py").read_text()
        self.assertIn("signal.SIGINT", backend)
        self.assertIn("app.quit()", backend)
        self.assertIn("setWindowIcon", backend)

        icon = module.parents[2] / "resources/icons/kayden.png"
        contents = icon.read_bytes()
        self.assertEqual(contents[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", contents[16:24])
        self.assertEqual(width, height)
        self.assertGreaterEqual(width, 512)

    def test_markdown_renderer_normalizes_lists_and_inline_code(self):
        rendered = render_markdown(
            "**Saved memories:**\n\n"
            "- `profile.name`: k5031\n"
            "- `profile.degree_subject`: Computer Science"
        )

        self.assertIn("<strong>Saved memories:</strong>", rendered)
        self.assertIn("<ul", rendered)
        self.assertEqual(rendered.count("<li"), 2)
        self.assertIn("font-size:13px", rendered)
        self.assertIn("profile.degree_subject", rendered)
        self.assertNotIn("background-color:#24283b", rendered)

    def test_markdown_renderer_escapes_model_supplied_html(self):
        rendered = render_markdown("<script>bad()</script> `safe`")

        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)


if __name__ == "__main__":
    unittest.main()
