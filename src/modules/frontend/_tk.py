import queue
from typing import Callable

from interfaces import FrontendInterface


class TkFrontend(FrontendInterface):
    """Shared Tk event plumbing for the desktop and floating frontends."""

    def __init__(self, *, title, geometry, floating=False):
        self.title = title
        self.geometry = geometry
        self.floating = floating
        self._events = queue.SimpleQueue()
        self._root = None
        self._assistant_open = False
        self._busy = False
        self._has_messages = False

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

    def _append(self, text, tag=None):
        self._transcript.configure(state="normal")
        self._transcript.insert("end", text, tag)
        self._transcript.configure(state="disabled")
        self._transcript.see("end")

    def _start_transcript(self):
        if self._has_messages:
            return
        self._transcript.configure(state="normal")
        self._transcript.delete("1.0", "end")
        self._transcript.configure(state="disabled")
        self._has_messages = True

    def _drain_events(self):
        while True:
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "user":
                self._start_transcript()
                self._assistant_open = False
                self._busy = True
                self._append(f"\nYou\n{value}\n", "user")
                self._status_var.set("Thinking…")
                self._stop_button.configure(state="normal")
            elif kind == "assistant":
                self._start_transcript()
                if not self._assistant_open:
                    self._append("\nKayden\n", "assistant_name")
                    self._assistant_open = True
                self._append(value, "assistant")
            elif kind == "status":
                self._status_var.set(value)
                if value.startswith(("[tool]", "[error]")):
                    if self._assistant_open:
                        self._append("\n")
                        self._assistant_open = False
                    self._append(f"\n  {value}\n", "status")
            elif kind == "finish":
                self._busy = False
                if self._assistant_open:
                    self._append("\n")
                if value:
                    self._append("  [interrupted]\n", "status")
                self._assistant_open = False
                self._status_var.set("Interrupted" if value else "Ready")
                self._stop_button.configure(state="disabled")
            elif kind == "close":
                self._root.destroy()
                return
        self._root.after(30, self._drain_events)

    def run(
        self,
        on_submit: Callable[[str], None],
        on_interrupt: Callable[[], None],
    ) -> None:
        try:
            import tkinter as tk
        except ImportError as error:
            raise RuntimeError(
                "this frontend requires Tk; install your system's python3-tk package"
            ) from error

        try:
            root = tk.Tk()
        except tk.TclError as error:
            raise RuntimeError(f"could not open a graphical display: {error}") from error

        self._root = root
        root.title(self.title)
        root.geometry(self.geometry)
        root.minsize(360, 220)
        root.configure(bg="#0b0d12")
        if self.floating:
            root.attributes("-topmost", True)

        frame = tk.Frame(root, bg="#0b0d12", padx=16, pady=14)
        frame.pack(fill="both", expand=True)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        header = tk.Frame(frame, bg="#0b0d12")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        tk.Label(
            header,
            text="KAYDEN",
            bg="#0b0d12",
            fg="#f3f6ff",
            font=("TkDefaultFont", 17, "bold"),
        ).pack(side="left")
        tk.Label(
            header,
            text="FLOATING ASSISTANT" if self.floating else "DESKTOP ASSISTANT",
            bg="#0b0d12",
            fg="#687087",
            font=("TkDefaultFont", 9, "bold"),
        ).pack(side="left", padx=(12, 0), pady=(5, 0))

        transcript_panel = tk.Frame(
            frame,
            bg="#11151d",
            highlightbackground="#2a3140",
            highlightthickness=1,
        )
        transcript_panel.grid(row=1, column=0, sticky="nsew")
        transcript_panel.grid_columnconfigure(0, weight=1)
        transcript_panel.grid_rowconfigure(0, weight=1)

        transcript = tk.Text(
            transcript_panel,
            wrap="word",
            state="disabled",
            bg="#11151d",
            fg="#e8eaf0",
            insertbackground="#e8eaf0",
            relief="flat",
            borderwidth=0,
            padx=16,
            pady=14,
            font=("TkDefaultFont", 11),
            spacing1=2,
            spacing3=3,
        )
        transcript.grid(row=0, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(
            transcript_panel,
            command=transcript.yview,
            bg="#252b38",
            troughcolor="#11151d",
            activebackground="#394257",
            relief="flat",
            borderwidth=0,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        transcript.configure(yscrollcommand=scrollbar.set)
        transcript.tag_configure("user", foreground="#7dcfff", font=("TkDefaultFont", 11, "bold"))
        transcript.tag_configure("assistant_name", foreground="#9ece6a", font=("TkDefaultFont", 11, "bold"))
        transcript.tag_configure("assistant", foreground="#f2f4f8")
        transcript.tag_configure("status", foreground="#8b93a7", font=("TkDefaultFont", 9))
        transcript.configure(state="normal")
        transcript.insert("end", "Kayden\n", "assistant_name")
        transcript.insert(
            "end",
            "Ready when you are. Type a message below to begin.\n",
            "assistant",
        )
        transcript.configure(state="disabled")
        self._transcript = transcript

        tk.Label(
            frame,
            text="MESSAGE",
            anchor="w",
            bg="#0b0d12",
            fg="#687087",
            font=("TkDefaultFont", 9, "bold"),
        ).grid(row=2, column=0, sticky="ew", pady=(12, 5))

        controls = tk.Frame(frame, bg="#0b0d12")
        controls.grid(row=3, column=0, sticky="ew")
        controls.grid_columnconfigure(0, weight=1)
        editor_height = 2 if self.floating else 3
        editor = tk.Text(
            controls,
            height=editor_height,
            wrap="word",
            bg="#171c25",
            fg="#f2f4f8",
            insertbackground="#f2f4f8",
            relief="flat",
            highlightbackground="#384156",
            highlightcolor="#7dcfff",
            highlightthickness=1,
            padx=8,
            pady=7,
            font=("TkDefaultFont", 11),
        )
        editor.grid(row=0, column=0, sticky="ew")

        buttons = tk.Frame(controls, bg="#0b0d12")
        buttons.grid(row=0, column=1, sticky="ns", padx=(10, 0))

        def submit(event=None):
            message = editor.get("1.0", "end-1c").strip()
            if message:
                editor.delete("1.0", "end")
                on_submit(message)
            return "break"

        def newline(event=None):
            editor.insert("insert", "\n")
            return "break"

        tk.Button(
            buttons,
            text="Send",
            command=submit,
            bg="#7dcfff",
            fg="#071018",
            activebackground="#a9ddff",
            activeforeground="#071018",
            relief="flat",
            borderwidth=0,
            padx=18,
            pady=7,
            cursor="hand2",
            font=("TkDefaultFont", 10, "bold"),
        ).pack(fill="x")
        stop_button = tk.Button(
            buttons,
            text="Stop",
            command=on_interrupt,
            bg="#292f3d",
            fg="#f0a3a3",
            activebackground="#3a2630",
            activeforeground="#ffc1c1",
            disabledforeground="#626a7b",
            relief="flat",
            borderwidth=0,
            padx=18,
            pady=7,
            cursor="hand2",
            font=("TkDefaultFont", 10),
            state="disabled",
        )
        stop_button.pack(
            fill="x", pady=(7, 0)
        )
        self._stop_button = stop_button
        editor.bind("<Return>", submit)
        editor.bind("<Shift-Return>", newline)

        self._status_var = tk.StringVar(value="Ready")
        tk.Label(
            frame,
            textvariable=self._status_var,
            anchor="w",
            bg="#0b0d12",
            fg="#8b93a7",
            font=("TkDefaultFont", 9),
        ).grid(row=4, column=0, sticky="ew", pady=(8, 0))

        closing = {"value": False}

        def close():
            if closing["value"]:
                return
            closing["value"] = True
            on_interrupt()
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", close)
        root.after(30, self._drain_events)
        editor.focus_set()
        root.mainloop()
