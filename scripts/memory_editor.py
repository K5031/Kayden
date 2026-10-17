import sys
import os
import tkinter as tk
from tkinter import ttk, messagebox
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kayden.config import BASE_DIR  # noqa: E402
import chromadb  # noqa: E402

CHROMA_PATH = os.path.join(BASE_DIR, "data", "chroma")
COLLECTION_NAME = "k_memory"

# Must match the embedder model used in mem0.py's config so vectors stay consistent.
EMBED_MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"

# ---------- palette ----------
BG = "#1e1f26"
BG_PANEL = "#262832"
BG_CARD = "#2e303c"
BG_CARD_HOVER = "#363946"
BG_CARD_SELECTED = "#3d5a80"
FG = "#e6e6ea"
FG_DIM = "#8b8d98"
FG_ACCENT = "#7dd3fc"
BORDER = "#3a3c48"
DANGER = "#f87171"
ACCENT = "#5b8def"
ACCENT_HOVER = "#4a78d9"

FONT_FAMILY = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"
MONO_FAMILY = "Consolas" if sys.platform.startswith("win") else "Courier"

PLACEHOLDER = "Search memories…"


def get_text(meta: dict) -> str:
    return meta.get("data") or meta.get("memory") or meta.get("text") or ""


class MemoryEditor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Memory Editor")
        self.geometry("1040x620")
        self.minsize(820, 480)
        self.configure(bg=BG)

        self.client = chromadb.PersistentClient(path=CHROMA_PATH)
        self.collection = None
        self.embedder = None  # lazy-loaded only when saving an edit
        self.current_id = None
        self.records = {}       # id -> {"text": ..., "meta": ...}
        self.card_widgets = {}  # id -> (card, inner, label, all_widgets)
        self.filter_var = tk.StringVar()

        self._build_style()
        self._build_ui()
        self.refresh()

    # ---------- style ----------

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG, font=(FONT_FAMILY, 10))

        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                         font=(FONT_FAMILY, 10, "bold"), padding=(14, 8), borderwidth=0)
        style.map("Accent.TButton", background=[("active", ACCENT_HOVER), ("disabled", "#4a4d5a")])

        style.configure("Ghost.TButton", background=BG_PANEL, foreground=FG,
                         font=(FONT_FAMILY, 10), padding=(10, 6), borderwidth=1)
        style.map("Ghost.TButton", background=[("active", BG_CARD_HOVER)])

        style.configure("Danger.TButton", background=BG_PANEL, foreground=DANGER,
                         font=(FONT_FAMILY, 10), padding=(10, 6), borderwidth=1)
        style.map("Danger.TButton", background=[("active", "#3a2020")])

        style.configure("Vertical.TScrollbar", background=BG_PANEL, troughcolor=BG,
                         bordercolor=BG, arrowcolor=FG_DIM)

    # ---------- UI ----------

    def _build_ui(self):
        topbar = tk.Frame(self, bg=BG, height=56)
        topbar.pack(fill="x", side="top")
        topbar.pack_propagate(False)

        title_wrap = tk.Frame(topbar, bg=BG)
        title_wrap.pack(side="left", padx=18, pady=10)
        tk.Label(title_wrap, text="Memory Editor", bg=BG, fg=FG,
                 font=(FONT_FAMILY, 15, "bold")).pack(anchor="w")
        tk.Label(title_wrap, text=COLLECTION_NAME, bg=BG, fg=FG_DIM,
                 font=(FONT_FAMILY, 9)).pack(anchor="w")

        self.count_var = tk.StringVar(value="")
        tk.Label(topbar, textvariable=self.count_var, bg=BG, fg=FG_ACCENT,
                 font=(FONT_FAMILY, 10, "bold")).pack(side="right", padx=18)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        body.columnconfigure(0, weight=0, minsize=320)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # ---- Left panel: search + card list ----
        left = tk.Frame(body, bg=BG_PANEL, highlightthickness=1, highlightbackground=BORDER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        search_row = tk.Frame(left, bg=BG_PANEL)
        search_row.pack(fill="x", padx=12, pady=12)
        self.search_entry = tk.Entry(search_row, bg=BG_CARD, fg=FG_DIM, insertbackground=FG,
                                      relief="flat", font=(FONT_FAMILY, 10))
        self.search_entry.pack(fill="x", ipady=6)
        self.search_entry.insert(0, PLACEHOLDER)
        self.search_entry.bind("<FocusIn>", self._clear_placeholder)
        self.search_entry.bind("<FocusOut>", self._restore_placeholder)
        self.search_entry.bind("<KeyRelease>", lambda e: self._render_list())

        list_container = tk.Frame(left, bg=BG_PANEL)
        list_container.pack(fill="both", expand=True, padx=(12, 4))

        canvas = tk.Canvas(list_container, bg=BG_PANEL, highlightthickness=0)
        vsb = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        self.cards_frame = tk.Frame(canvas, bg=BG_PANEL)

        self.cards_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.cards_frame, anchor="nw", width=284)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._bind_mousewheel(canvas)
        self.list_canvas = canvas

        btn_row = tk.Frame(left, bg=BG_PANEL)
        btn_row.pack(fill="x", padx=12, pady=12)
        ttk.Button(btn_row, text="Refresh", style="Ghost.TButton", command=self.refresh).pack(side="left")

        # ---- Right panel: detail / editor ----
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=3)
        right.rowconfigure(3, weight=2)
        right.columnconfigure(0, weight=1)

        tk.Label(right, text="MEMORY TEXT", bg=BG, fg=FG_DIM,
                 font=(FONT_FAMILY, 9, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6))

        text_wrap = tk.Frame(right, bg=BORDER, padx=1, pady=1)
        text_wrap.grid(row=1, column=0, sticky="nsew", pady=(0, 16))
        self.text_box = tk.Text(text_wrap, wrap="word", bg=BG_CARD, fg=FG, insertbackground=FG,
                                 relief="flat", font=(FONT_FAMILY, 11), padx=14, pady=12, undo=True)
        self.text_box.pack(fill="both", expand=True)

        tk.Label(right, text="METADATA", bg=BG, fg=FG_DIM,
                 font=(FONT_FAMILY, 9, "bold")).grid(row=2, column=0, sticky="w", pady=(0, 6))

        meta_wrap = tk.Frame(right, bg=BORDER, padx=1, pady=1)
        meta_wrap.grid(row=3, column=0, sticky="nsew", pady=(0, 16))
        self.meta_box = tk.Text(meta_wrap, wrap="word", bg=BG_PANEL, fg=FG_DIM,
                                 relief="flat", font=(MONO_FAMILY, 10), padx=14, pady=12, state="disabled")
        self.meta_box.pack(fill="both", expand=True)

        action_row = tk.Frame(right, bg=BG)
        action_row.grid(row=4, column=0, sticky="ew")
        self.save_btn = ttk.Button(action_row, text="Save changes (re-embeds)",
                                    style="Accent.TButton", command=self.save_selected)
        self.save_btn.pack(side="left")
        self.delete_btn = ttk.Button(action_row, text="Delete", style="Danger.TButton",
                                      command=self.delete_selected)
        self.delete_btn.pack(side="left", padx=8)

        self.save_status = tk.StringVar(value="")
        tk.Label(action_row, textvariable=self.save_status, bg=BG, fg=FG_DIM,
                 font=(FONT_FAMILY, 9)).pack(side="left", padx=10)

        self._set_editor_enabled(False)

    def _clear_placeholder(self, _e):
        if self.search_entry.get() == PLACEHOLDER:
            self.search_entry.delete(0, "end")
            self.search_entry.config(fg=FG)

    def _restore_placeholder(self, _e):
        if not self.search_entry.get():
            self.search_entry.insert(0, PLACEHOLDER)
            self.search_entry.config(fg=FG_DIM)

    def _bind_mousewheel(self, canvas):
        def _on_wheel(event):
            delta = -1 * int(event.delta / 120) if event.delta else (1 if event.num == 5 else -1)
            canvas.yview_scroll(delta, "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)
        canvas.bind_all("<Button-4>", _on_wheel)
        canvas.bind_all("<Button-5>", _on_wheel)

    def _set_editor_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.save_btn.config(state=state)
        self.delete_btn.config(state=state)
        self.text_box.config(state="normal" if enabled else "disabled")

    # ---------- data ----------

    def refresh(self):
        self.current_id = None
        self.text_box.config(state="normal")
        self.text_box.delete("1.0", "end")
        self._set_meta_text("")
        self._set_editor_enabled(False)
        self.records = {}

        try:
            self.collection = self.client.get_collection(COLLECTION_NAME)
        except Exception:
            self.count_var.set("no collection")
            self._render_list()
            return

        results = self.collection.get(include=["metadatas"])
        for id_, meta in zip(results["ids"], results["metadatas"]):
            meta = meta or {}
            self.records[id_] = {"text": get_text(meta), "meta": meta}

        self.count_var.set(f"{len(results['ids'])} memories")
        self._render_list()

    def _render_list(self):
        for child in self.cards_frame.winfo_children():
            child.destroy()
        self.card_widgets = {}

        query = self.search_entry.get().strip().lower()
        if query == PLACEHOLDER.lower():
            query = ""

        items = list(self.records.items())
        items.sort(key=lambda kv: kv[1]["meta"].get("created_at", ""), reverse=True)

        shown = 0
        for id_, record in items:
            if query and query not in record["text"].lower():
                continue
            shown += 1
            self._make_card(id_, record)

        if shown == 0:
            tk.Label(self.cards_frame, text="No memories found.", bg=BG_PANEL, fg=FG_DIM,
                     font=(FONT_FAMILY, 10), pady=20).pack(fill="x")

    def _make_card(self, id_, record):
        text = record["text"] or "(empty)"
        preview = text if len(text) <= 90 else text[:90].rstrip() + "…"
        created = record["meta"].get("created_at", "")
        date_str = created[:10] if created else ""

        card = tk.Frame(self.cards_frame, bg=BG_CARD, cursor="hand2")
        card.pack(fill="x", pady=4, padx=2)

        inner = tk.Frame(card, bg=BG_CARD)
        inner.pack(fill="x", padx=12, pady=10)

        label = tk.Label(inner, text=preview, bg=BG_CARD, fg=FG, font=(FONT_FAMILY, 10),
                          wraplength=250, justify="left", anchor="w")
        label.pack(fill="x", anchor="w")

        date_label = None
        if date_str:
            date_label = tk.Label(inner, text=date_str, bg=BG_CARD, fg=FG_DIM, font=(FONT_FAMILY, 8))
            date_label.pack(anchor="w", pady=(4, 0))

        widgets = [card, inner, label] + ([date_label] if date_label else [])
        for w in widgets:
            w.bind("<Button-1>", lambda e, i=id_: self._select_card(i))
            w.bind("<Enter>", lambda e, i=id_: self._hover_card(i, True))
            w.bind("<Leave>", lambda e, i=id_: self._hover_card(i, False))

        self.card_widgets[id_] = (card, inner, label, widgets)
        if id_ == self.current_id:
            self._highlight_card(id_)

    def _hover_card(self, id_, entering):
        if id_ == self.current_id:
            return
        self._paint_card(id_, BG_CARD_HOVER if entering else BG_CARD)

    def _paint_card(self, id_, color):
        if id_ not in self.card_widgets:
            return
        card, inner, label, widgets = self.card_widgets[id_]
        card.config(bg=color)
        inner.config(bg=color)
        for w in inner.winfo_children():
            w.config(bg=color)

    def _highlight_card(self, id_):
        for other_id in self.card_widgets:
            self._paint_card(other_id, BG_CARD)
        self._paint_card(id_, BG_CARD_SELECTED)

    def _set_meta_text(self, s: str):
        self.meta_box.config(state="normal")
        self.meta_box.delete("1.0", "end")
        self.meta_box.insert("1.0", s)
        self.meta_box.config(state="disabled")

    def _select_card(self, id_):
        self.current_id = id_
        self._highlight_card(id_)
        record = self.records[id_]

        self._set_editor_enabled(True)
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", record["text"])

        meta_display = "\n".join(
            f"{k}:  {v}" for k, v in record["meta"].items() if k not in ("data", "memory", "text")
        )
        self._set_meta_text(meta_display)
        self.save_status.set("")

    # ---------- actions ----------

    def delete_selected(self):
        if not self.current_id:
            return
        if not messagebox.askyesno("Delete memory", "Delete this memory permanently?"):
            return
        self.collection.delete(ids=[self.current_id])
        self.refresh()

    def save_selected(self):
        if not self.current_id:
            return

        new_text = self.text_box.get("1.0", "end").strip()
        if not new_text:
            messagebox.showwarning("Save", "Text can't be empty. Use Delete instead.")
            return

        self.save_status.set("Embedding…")
        self.save_btn.config(state="disabled")

        def worker():
            try:
                if self.embedder is None:
                    from sentence_transformers import SentenceTransformer
                    self.embedder = SentenceTransformer(EMBED_MODEL_NAME, device="cpu")

                vector = self.embedder.encode(new_text).tolist()

                meta = dict(self.records[self.current_id]["meta"])
                text_key = "data" if "data" in meta else ("memory" if "memory" in meta else "data")
                meta[text_key] = new_text
                meta["text_lemmatized"] = new_text

                self.collection.update(
                    ids=[self.current_id],
                    embeddings=[vector],
                    metadatas=[meta],
                )
                self.after(0, lambda: self._save_done(True))
            except Exception as e:
                self.after(0, lambda: self._save_done(False, str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _save_done(self, ok: bool, err: str = ""):
        self.save_btn.config(state="normal")
        if ok:
            self.save_status.set("Saved")
            self.refresh()
        else:
            self.save_status.set("Failed")
            messagebox.showerror("Save failed", err)


if __name__ == "__main__":
    app = MemoryEditor()
    app.mainloop()
