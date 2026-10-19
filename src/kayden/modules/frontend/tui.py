import curses
import queue
import sys
import textwrap
from typing import Callable

from kayden.interfaces import FrontendInterface


class Module(FrontendInterface):
    """A dependency-free, full-screen terminal frontend."""

    def __init__(self):
        self._assistant_name = "Kayden"
        self._events = queue.SimpleQueue()
        self._messages = []
        self._status = "Ready"
        self._busy = False
        self._closed = False
        self._scroll = 0
        self._on_submit = None
        self._on_interrupt = None
        self._editor = ""
        self._capture_label = "Capture"
        self._capture_state = "idle"
        self._on_capture_start = None
        self._on_capture_finish = None
        self._on_capture_cancel = None

    def configure_identity(self, assistant_name: str) -> None:
        self._assistant_name = assistant_name

    def user(self, message: str) -> None:
        self._events.put(("user", message))

    def assistant(self, token: str) -> None:
        self._events.put(("assistant", token))

    def status(self, message: str) -> None:
        self._events.put(("status", message))

    def finish(self, interrupted: bool = False) -> None:
        self._events.put(("finish", interrupted))

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
        self._capture_state = "idle"

    def capture_state(self, state: str) -> None:
        self._events.put(("capture_state", state))

    def draft(self, text: str) -> None:
        self._events.put(("draft", text))

    def stop(self) -> None:
        self._events.put(("close", None))

    def _drain_events(self):
        while True:
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                return
            if kind == "user":
                self._messages.append(["You", value])
                self._busy = True
                self._status = "Thinking…"
                self._scroll = 0
            elif kind == "assistant":
                if not self._messages or self._messages[-1][0] != self._assistant_name:
                    self._messages.append([self._assistant_name, ""])
                self._messages[-1][1] += value
                self._scroll = 0
            elif kind == "status":
                self._status = value
                if value.startswith(("[tool]", "[error]")):
                    role = "Tool" if value.startswith("[tool]") else "Error"
                    self._messages.append([role, value])
                    self._scroll = 0
            elif kind == "finish":
                self._busy = False
                self._status = "Interrupted" if value else "Ready"
            elif kind == "capture_state":
                self._capture_state = value
                self._status = {
                    "recording": "Recording…",
                    "transcribing": "Transcribing…",
                }.get(value, "Ready")
            elif kind == "draft":
                separator = " " if self._editor and not self._editor.endswith((" ", "\n")) else ""
                self._editor += separator + value
            elif kind == "close":
                self._closed = True

    @staticmethod
    def _safe_add(window, row, col, text, width, style=0):
        if row < 0 or width <= 0:
            return
        try:
            window.addnstr(row, col, text, width, style)
        except curses.error:
            pass

    def _transcript_lines(self, width):
        lines = []
        body_width = max(10, width - 4)
        for role, content in self._messages:
            role_style = 1 if role == "You" else 2 if role == self._assistant_name else 0
            lines.append((role, role_style))
            wrapped = []
            for paragraph in content.splitlines() or [""]:
                wrapped.extend(
                    textwrap.wrap(
                        paragraph,
                        width=body_width,
                        replace_whitespace=False,
                        drop_whitespace=False,
                    ) or [""]
                )
            lines.extend((line, 0) for line in wrapped)
            lines.append(("", 0))
        return lines

    def _draw(self, screen):
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 7 or width < 24:
            self._safe_add(screen, 0, 0, "Window too small", width - 1, curses.A_BOLD)
            screen.refresh()
            return

        title = f" {self._assistant_name.upper()} "
        self._safe_add(screen, 0, 2, title, width - 4, curses.A_BOLD)
        self._safe_add(screen, 1, 0, "─" * width, width)

        available = height - 5
        lines = self._transcript_lines(width)
        max_scroll = max(0, len(lines) - available)
        self._scroll = min(self._scroll, max_scroll)
        end = len(lines) - self._scroll
        start = max(0, end - available)
        visible = lines[start:end]
        for offset, (line, style_id) in enumerate(visible):
            style = curses.color_pair(style_id) | (curses.A_BOLD if style_id else 0)
            self._safe_add(screen, 2 + offset, 2, line, width - 4, style)

        divider = height - 3
        self._safe_add(screen, divider, 0, "─" * width, width)
        prompt = "› "
        shown = self._editor[-max(1, width - len(prompt) - 2):]
        self._safe_add(screen, divider + 1, 1, prompt + shown, width - 2)

        if self._capture_state == "recording":
            hint = "Ctrl-Y finish  •  Esc cancel"
        elif self._capture_state == "transcribing":
            hint = "Transcribing  •  Ctrl-D quit"
        elif self._on_capture_start:
            hint = "Enter send  •  Ctrl-Y mic  •  Ctrl-D quit"
        else:
            hint = "Esc stop  •  Ctrl-D quit" if self._busy else "Enter send  •  Ctrl-D quit"
        hint = hint[:max(1, width - 2)]
        hint_col = max(1, width - len(hint) - 1)
        status_width = max(1, hint_col - 2)
        self._safe_add(screen, height - 1, 1, self._status, status_width, curses.A_DIM)
        self._safe_add(screen, height - 1, hint_col, hint, len(hint), curses.A_DIM)

        cursor_col = min(width - 2, len(prompt) + len(shown) + 1)
        try:
            screen.move(divider + 1, cursor_col)
        except curses.error:
            pass
        screen.refresh()

    def _main(self, screen):
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        if curses.has_colors():
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
        screen.timeout(50)
        screen.keypad(True)
        self._editor = ""

        while not self._closed:
            self._drain_events()
            self._draw(screen)
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                if self._busy:
                    self._status = "Stopping…"
                    self._on_interrupt()
                    continue
                return

            if key in (curses.KEY_ENTER, "\n", "\r"):
                message = self._editor.strip()
                if message:
                    self._editor = ""
                    self._busy = True
                    self._status = "Queued…"
                    self._on_submit(message)
            elif key == "\x19" and self._on_capture_start:
                if self._capture_state == "recording":
                    self._on_capture_finish()
                elif self._capture_state == "idle":
                    self._on_capture_start()
            elif key in ("\x1b", "\x03"):
                if self._capture_state == "recording" and self._on_capture_cancel:
                    self._on_capture_cancel()
                elif self._busy:
                    self._status = "Stopping…"
                    self._on_interrupt()
                elif key == "\x03":
                    return
            elif key == "\x04" and not self._editor:
                return
            elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                self._editor = self._editor[:-1]
            elif key == curses.KEY_PPAGE:
                self._scroll += max(1, screen.getmaxyx()[0] - 6)
            elif key == curses.KEY_NPAGE:
                self._scroll = max(0, self._scroll - max(1, screen.getmaxyx()[0] - 6))
            elif isinstance(key, str) and key.isprintable():
                self._editor += key

    def run(
        self,
        on_submit: Callable[[str], None],
        on_interrupt: Callable[[], None],
    ) -> None:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("the tui frontend requires an interactive terminal")
        self._on_submit = on_submit
        self._on_interrupt = on_interrupt
        self._closed = False
        curses.wrapper(self._main)
