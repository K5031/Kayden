import signal
import sys
from pathlib import Path

from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from .markdown import render_markdown


class FrontendBridge(QObject):
    userReceived = Signal(str)
    assistantReceived = Signal(str)
    statusReceived = Signal(str)
    toolGroupReceived = Signal(int, str, str, str, int)
    finishReceived = Signal(bool)
    captureStateReceived = Signal(str)
    draftReceived = Signal(str)
    modelReceived = Signal(str, str)

    def __init__(self, frontend, parent=None):
        super().__init__(parent)
        self._frontend = frontend
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._poll)

    @Property(str, constant=True)
    def assistantName(self):
        return self._frontend.assistant_name

    @Property(int, constant=True)
    def windowWidth(self):
        return self._frontend.width

    @Property(int, constant=True)
    def windowHeight(self):
        return self._frontend.height

    @Property("QVariantList", constant=True)
    def models(self):
        return list(self._frontend._models)

    @Property(str, constant=True)
    def selectedModel(self):
        return self._frontend._selected_model or ""

    @Property(bool, constant=True)
    def captureAvailable(self):
        return self._frontend._on_capture_start is not None

    @Property(str, constant=True)
    def captureLabel(self):
        return self._frontend._capture_label

    def start(self):
        self._timer.start()
        self._poll()

    @Slot(str)
    def submit(self, message):
        self._frontend._submit(message)

    @Slot(str, result=str)
    def renderMarkdown(self, source):
        return render_markdown(source)

    @Slot()
    def interrupt(self):
        self._frontend._interrupt()

    @Slot(str)
    def selectModel(self, model):
        try:
            self._frontend._select_model(model)
        except Exception as error:
            self.statusReceived.emit(f"[error] {error}")

    @Slot()
    def startCapture(self):
        self._capture("start")

    @Slot()
    def finishCapture(self):
        self._capture("finish")

    @Slot()
    def cancelCapture(self):
        self._capture("cancel")

    def _capture(self, action):
        try:
            self._frontend._capture(action)
        except Exception as error:
            self.statusReceived.emit(f"[error] {error}")

    @Slot()
    def close(self):
        self._frontend._close_from_ui()

    @Slot()
    def _poll(self):
        for kind, value in self._frontend._drain_events():
            if kind == "user":
                self.userReceived.emit(value)
            elif kind == "assistant":
                self.assistantReceived.emit(value)
            elif kind == "status":
                self.statusReceived.emit(value)
            elif kind == "tool_group":
                self.toolGroupReceived.emit(*value)
            elif kind == "finish":
                self.finishReceived.emit(value)
            elif kind == "capture_state":
                self.captureStateReceived.emit(value)
            elif kind == "draft":
                self.draftReceived.emit(value)
            elif kind == "model":
                self.modelReceived.emit(*value)
            elif kind == "close":
                QGuiApplication.quit()


def run_qt(frontend):
    # Platform-native control themes can ignore parts of the QML palette and
    # produce mismatched light controls in an otherwise dark window. Basic is
    # fully styleable and behaves consistently across supported desktops.
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication.instance()
    if app is None:
        app = QGuiApplication(sys.argv[:1])

    engine = QQmlApplicationEngine()
    bridge = FrontendBridge(frontend, engine)
    engine.rootContext().setContextProperty("backend", bridge)
    view = Path(__file__).resolve().with_name("view.qml")
    engine.load(QUrl.fromLocalFile(str(view)))
    if not engine.rootObjects():
        raise RuntimeError(f"Qt could not load the frontend view at {view}")

    # Qt's native event loop can otherwise prevent Python from dispatching
    # SIGINT until some unrelated Python code runs. This lightweight timer
    # gives the interpreter regular opportunities to handle Ctrl-C.
    signal_pulse = QTimer(app)
    signal_pulse.setInterval(100)
    signal_pulse.timeout.connect(lambda: None)
    signal_pulse.start()

    previous_sigint = None

    def quit_from_terminal(_signum, _frame):
        frontend._close_from_ui()
        app.quit()

    try:
        previous_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, quit_from_terminal)
    except ValueError:
        # Embedders may run a frontend outside Python's main thread, where
        # signal handlers cannot be changed.
        previous_sigint = None

    bridge.start()
    try:
        app.exec()
    except KeyboardInterrupt:
        frontend._close_from_ui()
    finally:
        if previous_sigint is not None:
            signal.signal(signal.SIGINT, previous_sigint)
        signal_pulse.stop()
        bridge._timer.stop()
        frontend._close_from_ui()
