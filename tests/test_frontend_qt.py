from pathlib import Path
import unittest
from unittest.mock import Mock

from kayden.interfaces import FrontendInterface
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

    def test_queues_streaming_and_capture_events_before_event_loop_starts(self):
        frontend = QtFrontend()
        frontend.user("hello")
        frontend.assistant("Hi")
        frontend.status("[tool] current_time({})")
        frontend.finish()
        frontend.capture_state("recording")
        frontend.draft("voice text")

        self.assertEqual(list(frontend._drain_events()), [
            ("user", "hello"),
            ("assistant", "Hi"),
            ("status", "[tool] current_time({})"),
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

    def test_module_owns_its_qml_and_metadata(self):
        module = (
            Path(__file__).resolve().parents[1]
            / "src/kayden/modules/frontend/qt"
        )

        self.assertTrue((module / "view.qml").is_file())
        self.assertIn('name = "qt"', (module / "module.toml").read_text())
        qml = (module / "view.qml").read_text()
        self.assertIn("backend.selectModel", qml)
        self.assertIn("backend.startCapture", qml)
        self.assertIn("backend.interrupt", qml)
        self.assertIn("property real feedWidth", qml)
        self.assertIn("root.feedWidth", qml)
        self.assertIn("width - pageMargin * 2", qml)
        self.assertIn("Math.min(80, Math.max(20, width * 0.052))", qml)
        self.assertIn("property real assistantWidth: Math.min(1180, feedWidth)", qml)
        self.assertIn('x: kind === "user" ? parent.width - width : 0', qml)
        self.assertIn('modelOption.highlighted ? "#3b4261"', qml)

        backend = (module / "backend.py").read_text()
        self.assertIn("signal.SIGINT", backend)
        self.assertIn("app.quit()", backend)

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
