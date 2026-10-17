import curses
import queue
import sys
import textwrap
from typing import Callable

from kayden.interfaces import FrontendInterface


class Module(FrontendInterface):
    """A dependency-free, full-screen terminal frontend."""

    def __init__(self):
        self._events = queue.SimpleQueue()
        self._messages = []
        self._status = "Ready"
        self._busy = False
        self._closed = False
        self._scroll = 0
        self._on_submit = None
        self._on_interrupt = None

    def user(self, message: str) -> None:
        self._events.put(("user", message))

    def assistant(self, token: str) -> None:
        self._events.put(("assistant", token))

    def status(self, message: str) -> None:
        self._events.put(("status", message))

    def finish(self, interrupted: bool = False) -> None:
        self._events.put(("finish", interrupted))

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
                if not self._messages or self._messages[-1][0] != "Kayden":
                    self._messages.append(["Kayden", ""])
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
            role_style = {"You": 1, "Kayden": 2}.get(role, 0)
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

    def _draw(self, screen, editor):
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 7 or width < 24:
            self._safe_add(screen, 0, 0, "Window too small", width - 1, curses.A_BOLD)
            screen.refresh()
            return

        self._safe_add(screen, 0, 2, " KAYDEN ", width - 4, curses.A_BOLD)
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
        shown = editor[-max(1, width - len(prompt) - 2):]
        self._safe_add(screen, divider + 1, 1, prompt + shown, width - 2)

        hint = "Esc stop  •  Ctrl-D quit" if self._busy else "Enter send  •  Ctrl-D quit"
        status_width = max(1, width - len(hint) - 4)
        self._safe_add(screen, height - 1, 1, self._status, status_width, curses.A_DIM)
        self._safe_add(screen, height - 1, width - len(hint) - 1, hint, len(hint), curses.A_DIM)

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
        editor = ""

        while not self._closed:
            self._drain_events()
            self._draw(screen, editor)
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
                message = editor.strip()
                if message:
                    editor = ""
                    self._busy = True
                    self._status = "Queued…"
                    self._on_submit(message)
            elif key in ("\x1b", "\x03"):
                if self._busy:
                    self._status = "Stopping…"
                    self._on_interrupt()
                elif key == "\x03":
                    return
            elif key == "\x04" and not editor:
                return
            elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                editor = editor[:-1]
            elif key == curses.KEY_PPAGE:
                self._scroll += max(1, screen.getmaxyx()[0] - 6)
            elif key == curses.KEY_NPAGE:
                self._scroll = max(0, self._scroll - max(1, screen.getmaxyx()[0] - 6))
            elif isinstance(key, str) and key.isprintable():
                editor += key

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
