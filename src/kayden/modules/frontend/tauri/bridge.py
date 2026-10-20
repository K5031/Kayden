import json
import os
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from kayden.frontend_api import (
    AssistantText,
    CancelCapture,
    CaptureConfigured,
    CaptureStateChanged,
    ControlsConfigured,
    DraftChanged,
    FinishCapture,
    IdentityConfigured,
    InterruptTurn,
    InvokeControl,
    ModelsConfigured,
    SelectModel,
    StartCapture,
    StatusChanged,
    SubmitMessage,
    ToolActivityFinished,
    ToolActivityStarted,
    TurnFinished,
    UserMessage,
)

MAX_COMMAND_BYTES = 64 * 1024


class _BridgeServer(ThreadingHTTPServer):
    daemon_threads = True


class DesktopFrontend:
    """Shared bridge between Kayden's Python runtime and its Tauri client."""

    def __init__(self, *, mode, title, width, height, floating=False):
        self.mode = mode
        self.title = title
        self.assistant_name = "Kayden"
        self.width = width
        self.height = height
        self.geometry = f"{width}x{height}"
        self.floating = floating
        self._capture_label = "Capture"
        self._models = []
        self._selected_model = None
        self._controls = []
        self._on_model_select = None
        self._on_capture_start = None
        self._on_capture_finish = None
        self._on_capture_cancel = None
        self._on_submit = None
        self._on_interrupt = None
        self._condition = threading.Condition()
        self._event_log = deque(maxlen=10_000)
        self._next_event_id = 1
        self._closed = threading.Event()
        self._server = None
        self._server_thread = None
        self._process = None
        self._token = None
        self._bridge_url = None
        self._api = None
        self._unsubscribe = None

    def _accept_event(self, event):
        if isinstance(event, IdentityConfigured):
            self.configure_identity(event.assistant_name)
        elif isinstance(event, ModelsConfigured):
            self.configure_models(
                event.models,
                event.selected_model,
                lambda model: self._api.send(SelectModel(model)),
            )
        elif isinstance(event, CaptureConfigured):
            if event.available:
                self.configure_capture(
                    lambda: self._api.send(StartCapture()),
                    lambda: self._api.send(FinishCapture()),
                    lambda: self._api.send(CancelCapture()),
                    event.label,
                )
            else:
                self.clear_capture()
        elif isinstance(event, ControlsConfigured):
            self.configure_controls(event.controls)
        elif isinstance(event, UserMessage):
            self.user(event.text)
        elif isinstance(event, AssistantText):
            self.assistant(event.text)
        elif isinstance(event, StatusChanged):
            self.status(event.text)
        elif isinstance(event, ToolActivityStarted):
            arguments = json.dumps(event.arguments, ensure_ascii=False)
            self.status(f"[tool] {event.name}({arguments})")
        elif isinstance(event, ToolActivityFinished):
            if event.result.is_error:
                self.status(f"[tool] {event.name} failed: {event.result.content}")
            else:
                self.status(f"[tool] {event.name} finished")
        elif isinstance(event, TurnFinished):
            self.finish(event.interrupted)
        elif isinstance(event, CaptureStateChanged):
            self.capture_state(event.state)
        elif isinstance(event, DraftChanged):
            self.draft(event.text)

    def configure_identity(self, assistant_name: str) -> None:
        self.assistant_name = assistant_name
        self.title = (
            f"{assistant_name} · Floaty" if self.floating else assistant_name
        )

    def configure_models(self, models, selected_model, on_select) -> bool:
        normalized = []
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            name = model.get("name")
            if not isinstance(model_id, str) or not model_id:
                continue
            normalized.append({
                "id": model_id,
                "name": name if isinstance(name, str) and name else model_id,
            })
        if not normalized or selected_model not in {
            model["id"] for model in normalized
        }:
            return False
        self._models = normalized
        self._selected_model = selected_model
        self._on_model_select = on_select
        return True

    def _emit(self, kind, **payload):
        with self._condition:
            event = {"id": self._next_event_id, "type": kind, **payload}
            self._next_event_id += 1
            self._event_log.append(event)
            self._condition.notify_all()
        return event

    def user(self, message: str) -> None:
        self._emit("user", message=message)

    def assistant(self, token: str) -> None:
        self._emit("assistant", text=token)

    def status(self, message: str) -> None:
        self._emit("status", message=message)

    def finish(self, interrupted: bool = False) -> None:
        self._emit("finish", interrupted=interrupted)

    def configure_capture(
        self,
        on_start: Callable[[], None],
        on_finish: Callable[[], None],
        on_cancel: Callable[[], None],
        label: str = "Capture",
    ) -> bool:
        self._on_capture_start = on_start
        self._on_capture_finish = on_finish
        self._on_capture_cancel = on_cancel
        self._capture_label = label
        return True

    def clear_capture(self) -> None:
        self._on_capture_start = None
        self._on_capture_finish = None
        self._on_capture_cancel = None
        self._capture_label = "Capture"
        self._emit("capture_config", available=False, label=self._capture_label)

    def configure_controls(self, controls) -> None:
        self._controls = [dict(control) for control in controls]
        self._emit("controls", controls=list(self._controls))

    def capture_state(self, state: str) -> None:
        self._emit("capture_state", state=state)

    def draft(self, text: str) -> None:
        self._emit("draft", text=text)

    def _events_after(self, event_id, timeout=15):
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._closed.is_set():
                events = [event for event in self._event_log if event["id"] > event_id]
                if events:
                    return events
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(remaining)
        return []

    def _configuration(self):
        return {
            "mode": self.mode,
            "title": self.title,
            "width": self.width,
            "height": self.height,
            "floating": self.floating,
            "assistantName": self.assistant_name,
            "capture": self._on_capture_start is not None,
            "captureLabel": self._capture_label,
            "models": list(self._models),
            "selectedModel": self._selected_model,
            "controls": list(self._controls),
        }

    def _command(self, path, payload):
        if path == "/commands/submit":
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                raise ValueError("message must be non-empty text")
            self._on_submit(message.strip())
        elif path == "/commands/interrupt":
            self._on_interrupt()
        elif path == "/commands/model" and self._on_model_select:
            model = payload.get("model")
            if not isinstance(model, str) or model not in {
                option["id"] for option in self._models
            }:
                raise ValueError("model is unavailable")
            self._on_model_select(model)
            self._selected_model = model
            selected = next(
                option for option in self._models if option["id"] == model
            )
            self._emit("model", model=model, label=selected["name"])
        elif path == "/commands/capture/start" and self._on_capture_start:
            self._on_capture_start()
        elif path == "/commands/capture/finish" and self._on_capture_finish:
            self._on_capture_finish()
        elif path == "/commands/capture/cancel" and self._on_capture_cancel:
            self._on_capture_cancel()
        elif path == "/commands/control" and self._api:
            control_id = payload.get("id")
            available = {control["id"] for control in self._controls}
            if not isinstance(control_id, str) or control_id not in available:
                raise ValueError("control is unavailable")
            self._api.send(InvokeControl(control_id, payload.get("value")))
        elif path == "/commands/close":
            if self._on_capture_cancel:
                self._on_capture_cancel()
            self._on_interrupt()
            self._closed.set()
            with self._condition:
                self._condition.notify_all()
        else:
            raise ValueError("unknown or unavailable command")

    def _handler_class(self):
        frontend = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format, *_args):
                return

            def _headers(self, status, content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header(
                    "Access-Control-Allow-Headers",
                    "Authorization, Content-Type, X-Kayden-Last-Event",
                )
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

            def _authorized(self):
                return self.headers.get("Authorization") == f"Bearer {frontend._token}"

            def _json(self, status, body):
                encoded = json.dumps(body).encode("utf-8")
                try:
                    self._headers(status)
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    # The desktop client can close its long-poll socket while the
                    # bridge is completing a response during normal shutdown.
                    return

            def do_OPTIONS(self):
                self._headers(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if not self._authorized():
                    self._json(401, {"error": "unauthorized"})
                    return
                if self.path == "/config":
                    self._json(200, frontend._configuration())
                    return
                if self.path != "/events":
                    self._json(404, {"error": "not found"})
                    return

                try:
                    last_id = int(self.headers.get("X-Kayden-Last-Event", "0"))
                except ValueError:
                    self._json(400, {"error": "invalid event cursor"})
                    return

                events = frontend._events_after(last_id)
                self._json(200, {"events": events})

            def do_POST(self):
                if not self._authorized():
                    self._json(401, {"error": "unauthorized"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._json(400, {"error": "invalid content length"})
                    return
                if length > MAX_COMMAND_BYTES:
                    self._json(413, {"error": "command is too large"})
                    return
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(payload, dict):
                        raise ValueError("request body must be an object")
                    frontend._command(self.path, payload)
                except (json.JSONDecodeError, ValueError) as error:
                    self._json(400, {"error": str(error)})
                    return
                except Exception as error:
                    self._json(500, {"error": str(error)})
                    return
                self._json(200, {"ok": True})

        return Handler

    def _start_server(self):
        if self._server:
            return
        self._token = secrets.token_urlsafe(32)
        self._server = _BridgeServer(("127.0.0.1", 0), self._handler_class())
        port = self._server.server_address[1]
        self._bridge_url = f"http://127.0.0.1:{port}"
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="kayden-desktop-bridge",
            daemon=True,
        )
        self._server_thread.start()

    @staticmethod
    def _desktop_source():
        path = Path(__file__).resolve().parent / "desktop"
        return path if (path / "package.json").is_file() else None

    def _launch_command(self):
        configured = os.environ.get("KAYDEN_DESKTOP_COMMAND")
        if configured:
            return shlex.split(configured), None
        binary = os.environ.get("KAYDEN_DESKTOP_BINARY") or shutil.which(
            "kayden-desktop"
        )
        if binary:
            return [binary], None
        source = self._desktop_source()
        if source and shutil.which("npm") and shutil.which("cargo"):
            return ["npm", "run", "tauri", "dev"], source
        raise RuntimeError(
            "Kayden's Tauri frontend module has no runnable desktop client. "
            "Install its platform package, set KAYDEN_DESKTOP_BINARY, or install "
            "Node.js and Rust to run the client from the module source."
        )

    def run(
        self,
        api,
    ) -> None:
        self._api = api
        self._on_submit = lambda text: api.send(SubmitMessage(text))
        self._on_interrupt = lambda: api.send(InterruptTurn())
        self._unsubscribe = api.subscribe(self._accept_event)
        self._closed.clear()
        try:
            self._start_server()
            command, cwd = self._launch_command()
            environment = os.environ.copy()
            environment.update({
                "KAYDEN_BRIDGE_URL": self._bridge_url,
                "KAYDEN_BRIDGE_TOKEN": self._token,
                "KAYDEN_WINDOW_MODE": self.mode,
                "KAYDEN_ASSISTANT_NAME": self.assistant_name,
            })
            self._process = subprocess.Popen(command, cwd=cwd, env=environment)
            while self._process.poll() is None and not self._closed.wait(0.1):
                pass
        finally:
            self._closed.set()
            with self._condition:
                self._condition.notify_all()
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            if self._server:
                self._server.shutdown()
                self._server.server_close()
                self._server = None
            self._unsubscribe()
            self._unsubscribe = None
            self._api = None

    def stop(self) -> None:
        self._closed.set()
        with self._condition:
            self._condition.notify_all()
        if self._process and self._process.poll() is None:
            self._process.terminate()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
