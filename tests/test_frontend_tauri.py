import os
from pathlib import Path
import re
import unittest
from unittest.mock import Mock, patch

from kayden.interfaces import FrontendInterface
from kayden.modules.frontend.tauri import Module as TauriFrontend


class TauriFrontendTests(unittest.TestCase):
    @staticmethod
    def _style_rule(selector):
        stylesheet = (
            Path(__file__).resolve().parents[1]
            / "src/kayden/modules/frontend/tauri/desktop/src/styles.css"
        ).read_text()
        match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", stylesheet)
        if match is None:
            raise AssertionError(f"missing style rule for {selector}")
        return match.group(1)

    def test_defaults_to_full_desktop_window(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KAYDEN_TAURI_MODE", None)
            frontend = TauriFrontend()

        self.assertIsInstance(frontend, FrontendInterface)
        self.assertEqual(frontend.title, "Kayden")
        self.assertEqual(frontend.mode, "gui")
        self.assertEqual(frontend.geometry, "1000x760")
        self.assertFalse(frontend.floating)

    def test_supports_compact_always_on_top_mode(self):
        frontend = TauriFrontend(mode="floaty")

        self.assertEqual(frontend.title, "Kayden · Floaty")
        self.assertEqual(frontend.mode, "floaty")
        self.assertEqual(frontend.geometry, "560x440")
        self.assertTrue(frontend.floating)

    def test_environment_can_override_configured_mode(self):
        with patch.dict(os.environ, {"KAYDEN_TAURI_MODE": "floaty"}):
            frontend = TauriFrontend(mode="gui")

        self.assertEqual(frontend.mode, "floaty")

    def test_rejects_unknown_window_mode(self):
        with patch.dict(os.environ, {"KAYDEN_TAURI_MODE": "tiny"}):
            with self.assertRaisesRegex(ValueError, "gui.*floaty"):
                TauriFrontend()

    def test_public_events_are_marshaled_to_desktop_bridge(self):
        frontend = TauriFrontend()
        frontend.user("hello")
        frontend.assistant("hi")
        frontend.finish()

        self.assertEqual(list(frontend._event_log), [
            {"id": 1, "type": "user", "message": "hello"},
            {"id": 2, "type": "assistant", "text": "hi"},
            {"id": 3, "type": "finish", "interrupted": False},
        ])

    def test_manual_capture_callbacks_and_drafts_are_marshaled(self):
        frontend = TauriFrontend()
        callbacks = [lambda: None, lambda: None, lambda: None]

        self.assertTrue(frontend.configure_capture(*callbacks, label="Mic"))
        frontend.capture_state("recording")
        frontend.draft("hello world")

        self.assertEqual(frontend._capture_label, "Mic")
        self.assertEqual(list(frontend._event_log), [
            {"id": 1, "type": "capture_state", "state": "recording"},
            {"id": 2, "type": "draft", "text": "hello world"},
        ])

    def test_exposes_configured_assistant_identity_to_desktop(self):
        frontend = TauriFrontend()
        frontend.configure_identity("Ahri")

        self.assertEqual(frontend.title, "Ahri")
        self.assertEqual(frontend._configuration()["assistantName"], "Ahri")

    def test_exposes_available_models_and_routes_model_selection(self):
        frontend = TauriFrontend()
        selected = []
        models = [
            {"id": "gpt-fast", "name": "GPT Fast"},
            {"id": "gpt-deep", "name": "GPT Deep"},
        ]

        self.assertTrue(
            frontend.configure_models(models, "gpt-fast", selected.append)
        )
        self.assertEqual(frontend._configuration()["models"], models)
        self.assertEqual(frontend._configuration()["selectedModel"], "gpt-fast")

        frontend._command("/commands/model", {"model": "gpt-deep"})

        self.assertEqual(selected, ["gpt-deep"])
        self.assertEqual(frontend._configuration()["selectedModel"], "gpt-deep")
        self.assertEqual(list(frontend._event_log), [{
            "id": 1,
            "type": "model",
            "model": "gpt-deep",
            "label": "GPT Deep",
        }])

    def test_rejects_model_selection_outside_advertised_options(self):
        frontend = TauriFrontend()
        frontend.configure_models(
            [{"id": "gpt-fast", "name": "GPT Fast"}],
            "gpt-fast",
            lambda model: None,
        )

        with self.assertRaisesRegex(ValueError, "unavailable"):
            frontend._command("/commands/model", {"model": "gpt-hidden"})

    def test_bridge_binds_only_to_loopback_and_routes_commands(self):
        frontend = TauriFrontend()
        submitted = []
        interrupted = []
        frontend._desktop._on_submit = submitted.append
        frontend._desktop._on_interrupt = lambda: interrupted.append(True)
        with patch(
            "kayden.modules.frontend.tauri.bridge._BridgeServer"
        ) as server_factory:
            server_factory.return_value.server_address = ("127.0.0.1", 43120)
            frontend._start_server()

        self.assertEqual(server_factory.call_args.args[0], ("127.0.0.1", 0))
        self.assertTrue(frontend._token)
        self.assertEqual(frontend._bridge_url, "http://127.0.0.1:43120")

        frontend._command("/commands/submit", {"message": " hello "})
        frontend._command("/commands/interrupt", {})
        self.assertEqual(submitted, ["hello"])
        self.assertEqual(interrupted, [True])
        frontend.stop()

    def test_bridge_rejects_requests_without_its_random_token(self):
        frontend = TauriFrontend()
        frontend._desktop._token = "secret"
        handler_class = frontend._handler_class()
        handler = object.__new__(handler_class)

        handler.headers = {}
        self.assertFalse(handler._authorized())
        handler.headers = {"Authorization": "Bearer wrong"}
        self.assertFalse(handler._authorized())
        handler.headers = {"Authorization": "Bearer secret"}
        self.assertTrue(handler._authorized())

    def test_bridge_ignores_client_disconnect_while_writing_response(self):
        frontend = TauriFrontend()
        handler_class = frontend._handler_class()
        handler = object.__new__(handler_class)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.wfile.write.side_effect = BrokenPipeError

        handler._json(200, {"events": []})

        handler.wfile.write.assert_called_once()

    def test_stop_closes_the_desktop_bridge(self):
        frontend = TauriFrontend()
        frontend.stop()
        self.assertTrue(frontend._closed.is_set())

    def test_long_transcripts_scroll_without_pushing_composer_offscreen(self):
        application = self._style_rule(".app")
        conversation = self._style_rule(".conversation")
        transcript = self._style_rule(".transcript")

        self.assertIn("overflow: hidden", application)
        self.assertIn("height: 100%", conversation)
        self.assertIn("min-height: 0", conversation)
        self.assertIn("overflow: hidden", conversation)
        self.assertIn("min-height: 0", transcript)
        self.assertIn("overflow: auto", transcript)


if __name__ == "__main__":
    unittest.main()
