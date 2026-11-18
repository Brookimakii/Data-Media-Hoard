"""
ui/tagger_page.py
-----------------
Tagger: tag many images at once.

    ┌ folder  [Browse] [Load]                                   N images · M selected ┐
    │ [All] [Untagged] [Tagged] [Needs attention]   search: dog -cat wol*      [↻]    │
    ├──────────────────────────────────────────────┬──────────────────────────────────┤
    │  thumbnail grid (click, Ctrl/Shift+click,    │  TAGS of the selection (click to │
    │  Ctrl+A)  — double-click opens the image     │  remove) · add field with        │
    │  large (←/→ next / previous)                 │  autocomplete · Fix tags         │
    │                                              │  AI SUGGESTIONS (click to accept)│
    └──────────────────────────────────────────────┴──────────────────────────────────┘

Every change is saved at once (sidecar .txt + hoard.db, as before) and Ctrl+Z undoes it.
AI suggestions only propose: nothing is written until you click one.
All logic lives in core.tagging / core.tagger_data / core.ai_tagger.

The model that suggests tags is chosen in the page's ⚙ settings (core.tagger_settings).
"Enlarge" (Space, or the button) opens the selection in a big viewer like the duplicate
finder's: wheel = zoom at the cursor, drag = move, F = fit, 1 = 100 %, ←/→ or PgUp/PgDn =
previous / next image (inside the selection when several are selected).
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import tkinter as tk
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from tkinter import filedialog

from PIL import Image, ImageTk

from core import ai_tagger, tagger_data as td, tagger_settings, tagging
from core.catalogue import load_config
from core.dupes_meta import View, fit_scale
from core.file_db import get_db
from core.tag_store import TagStore
from ui import back_stack
from ui.artists_page import _Button, _caption, _entry
from ui.autocomplete import Autocomplete
from ui.canvas_list import CanvasList
from ui.dupes_compare import _Pane
from ui.tags_page import TYPE_COLOR
from ui.theme import (
    ACCENT, BORDER, COLOR_FAIL, COLOR_OK, ENTRY_BG, FG, FG_DIM, FONT_BTN, FONT_SUB, PANEL, SEL_BG,
)

_UI = FONT_BTN[0]
_CELL = 164                      # grid cell (thumbnail 150 + gap)
_PANEL_W = 340
_AMBER = "#d9a441"
_POLL_MS = 120
_PHOTO_CACHE = 500
_MAX_AGG = 300                   # images analysed for the suggestion list at most
_FILTERS = (("all", "All"), ("untagged", "Untagged"), ("tagged", "Tagged"),
            ("attention", "Needs attention"))

_ACTIVE: "TaggerPage | None" = None
_KEYS_BOUND = False
_KEYS = (("<Left>", "left"), ("<Right>", "right"), ("<Up>", "up"), ("<Down>", "down"),
         ("<Return>", "enter"), ("<Escape>", "escape"), ("<Control-a>", "all"),
         ("<Control-A>", "all"), ("<Control-z>", "undo"), ("<Control-Z>", "undo"),
         ("<space>", "enlarge"), ("<Prior>", "prev"), ("<Next>", "next"),
         ("<Home>", "first"), ("<End>", "last"), ("<Key-f>", "fit"), ("<Key-1>", "actual"),
         ("<plus>", "zoomin"), ("<equal>", "zoomin"), ("<minus>", "zoomout"))


def _activate(page: "TaggerPage") -> None:
    global _ACTIVE, _KEYS_BOUND
    _ACTIVE = page
    if not _KEYS_BOUND:
        _KEYS_BOUND = True
        for seq, name in _KEYS:
            page.bind_all(seq, lambda _e, n=name: _ACTIVE and _ACTIVE.on_key(n), add="+")


def _download_base() -> Path:
    root = Path(__file__).resolve().parent.parent
    try:
        cfg = load_config(str(root / "config.yaml"))
        return (root / cfg.get("download_dir", "./download")).resolve()
    except Exception:
        return (root / "download").resolve()


# ── Clickable chips ──────────────────────────────────────────────────────────

class _Chips(tk.Text):
    """A wrapping row of clickable labels (no scrollbar; the wheel scrolls)."""

    def __init__(self, parent: tk.Misc, on_click, height: int) -> None:
        super().__init__(parent, bg=PANEL, fg=FG, relief="flat", highlightthickness=0,
                         height=height, wrap="word", cursor="arrow", font=(_UI, 10),
                         padx=2, pady=2, state="disabled", spacing1=2, spacing3=2)
        self._on_click = on_click

    def set(self, chips: list[tuple[str, str, str]], empty: str = "") -> None:
        """chips: (label, key passed to on_click, colour)."""
        self.config(state="normal")
        self.delete("1.0", "end")
        if not chips and empty:
            self.insert("end", empty, ("hint",))
            self.tag_config("hint", foreground=FG_DIM, font=FONT_SUB)
        for n, (label, key, color) in enumerate(chips):
            tag = f"c{n}"
            self.insert("end", f" {label} ", (tag,))
            self.insert("end", " ")
            self.tag_config(tag, foreground=color, background=ENTRY_BG)
            self.tag_bind(tag, "<Button-1>", lambda _e, k=key: self._on_click(k))
            self.tag_bind(tag, "<Enter>", lambda _e, t=tag: (
                self.tag_config(t, background=SEL_BG), self.config(cursor="hand2")))
            self.tag_bind(tag, "<Leave>", lambda _e, t=tag: (
                self.tag_config(t, background=ENTRY_BG), self.config(cursor="arrow")))
        self.config(state="disabled")
        self.yview_moveto(0)


class TaggerPage(tk.Frame):
    def __init__(self, parent: tk.Misc, db, store: TagStore, db_path: str | None) -> None:
        super().__init__(parent, bg=PANEL)
        self._db, self._store = db, store
        self._db_path = db_path
        self._base = _download_base()
        self._paths: list[Path] = []
        self._tags: dict[Path, list[str]] = {}
        self._view: list[Path] = []
        self._index: dict[Path, int] = {}
        self._sel = td.Selection()
        self._per_row = 1
        self._undo: list[tuple[str, list[tagging.TagEdit]]] = []
        self._jpgs: dict[Path, bytes | None] = {}
        self._photos: "OrderedDict[Path, ImageTk.PhotoImage]" = OrderedDict()
        self._canon: dict[str, str | None] = {}
        self._closure: dict[str, list[str]] = {}
        self._scores: dict[Path, dict[str, float]] = {}
        self._screen = "grid"
        self._viewing: Path | None = None
        self._view_photo = None
        self._panel_job: str | None = None
        self._loading: "queue.Queue | None" = None
        self._busy_msg = ""

        self._thumbs = td.ThumbStore(db_path) if db_path else None
        self._loader = td.ThumbLoader(self._thumbs) if self._thumbs else None
        self._order: list[Path] = []                 # images the viewer steps through
        self._vview = View()
        self._ticks = 0
        self._download_msg = ""
        self._model_sig: tuple | None = None
        self._use_model(tagger_settings.load())

        self._build()
        back_stack.register(self.handle_back)
        _activate(self)
        self.after(_POLL_MS, self._poll)

    def _use_model(self, settings: tagger_settings.TaggerSettings) -> None:
        """(Re)create the AI engine for the model chosen in the ⚙ settings."""
        spec = tagger_settings.active_spec(settings)
        self._spec = spec
        self._model_sig = settings.signature()
        self._ai_cache = ai_tagger.ScoreStore(self._db.connection, spec.key)
        db_path = self._db_path
        self._ai = td.AIJobs(
            ai_tagger.AITagger(spec),
            lambda: ai_tagger.ScoreStore(sqlite3.connect(db_path, timeout=10), spec.key))
        self._scores = {}
        self._download_msg = ""

    # ══ Layout ════════════════════════════════════════════════════════════════

    def _build(self) -> None:
        pad = {"padx": 20}
        top = tk.Frame(self, bg=PANEL)
        top.pack(fill="x", pady=(14, 6), **pad)
        self._v_folder = tk.StringVar(value=str(self._base))
        _entry(top, self._v_folder).pack(side="left", fill="x", expand=True, ipady=4)
        _Button(top, "Browse…", self._browse).pack(side="left", padx=(8, 0))
        _Button(top, "Load", self._load, "primary").pack(side="left", padx=(8, 0))
        self._count = tk.Label(top, text="", bg=PANEL, fg=FG_DIM, font=FONT_SUB)
        self._count.pack(side="left", padx=(14, 0))

        bar = tk.Frame(self, bg=PANEL)
        bar.pack(fill="x", pady=(0, 8), **pad)
        self._v_filter = tk.StringVar(value="all")
        self._filter_btns: dict[str, tk.Label] = {}
        for key, label in _FILTERS:
            b = tk.Label(bar, text=label, bg=ENTRY_BG, fg=FG, font=FONT_BTN, padx=12, pady=5,
                         cursor="hand2")
            b.pack(side="left", padx=(0, 6))
            b.bind("<Button-1>", lambda _e, k=key: self._set_filter(k))
            self._filter_btns[key] = b
        tk.Label(bar, text="Search", bg=PANEL, fg=FG_DIM, font=FONT_SUB).pack(side="left",
                                                                            padx=(10, 6))
        self._v_query = tk.StringVar()
        q = _entry(bar, self._v_query)
        q.pack(side="left", fill="x", expand=True, ipady=4)
        self._v_query.trace_add("write", lambda *_a: self._query_changed())
        _Button(bar, "↻", self._recheck).pack(side="left", padx=(8, 0))
        self._style_filters()

        body = tk.Frame(self, bg=PANEL)
        body.pack(fill="both", expand=True, padx=20, pady=(0, 6))
        self._stage = tk.Frame(body, bg=PANEL)
        self._stage.pack(side="left", fill="both", expand=True)
        side = tk.Frame(body, bg=PANEL, width=_PANEL_W)
        side.pack(side="right", fill="y", padx=(14, 0))
        side.pack_propagate(False)
        self._build_grid()
        self._build_viewer()
        self._build_panel(side)
        self._status = tk.Label(self, text="Pick a folder and press Load.", bg=PANEL, fg=FG_DIM,
                                font=FONT_SUB, anchor="w")
        self._status.pack(fill="x", padx=20, pady=(0, 8))
        self._show("grid")

    def _build_grid(self) -> None:
        self._grid = CanvasList(self._stage, _CELL, self._draw_row, self._grid_click,
                                self._grid_double, PANEL)
        self._grid.canvas.bind("<Configure>", lambda _e: self._on_resize(), add="+")
        self._grid.canvas.bind("<Button-1>", lambda _e: self._grid.canvas.focus_set(), add="+")

    def _build_viewer(self) -> None:
        self._viewer = tk.Frame(self._stage, bg=PANEL)
        head = tk.Frame(self._viewer, bg=PANEL)
        head.pack(fill="x", pady=(0, 4))
        _Button(head, "‹  Grid", self._to_grid).pack(side="left")
        self._v_name = tk.Label(head, text="", bg=PANEL, fg=FG, font=FONT_SUB, anchor="w")
        self._v_name.pack(side="left", padx=12, fill="x", expand=True)
        _Button(head, "Next  ›", lambda: self._step(1)).pack(side="right")
        _Button(head, "‹  Prev", lambda: self._step(-1)).pack(side="right", padx=(0, 6))
        _Button(head, "100%", self._actual).pack(side="right", padx=(0, 14))
        _Button(head, "Fit", self._fit).pack(side="right", padx=(0, 4))
        _Button(head, "＋", lambda: self._zoom(1.4)).pack(side="right", padx=(0, 4))
        _Button(head, "－", lambda: self._zoom(1 / 1.4)).pack(side="right", padx=(0, 4))
        self._v_tags = tk.Label(self._viewer, text="", bg=PANEL, fg=FG_DIM, font=FONT_SUB,
                                anchor="w", justify="left")
        self._v_tags.pack(fill="x", pady=(0, 4))
        self._v_tags.bind("<Configure>", lambda e: self._v_tags.config(wraplength=e.width))
        self._pane = _Pane(self._viewer, self._vview, lambda pane: pane.request_redraw())
        self._pane.pack(fill="both", expand=True)

    def _build_panel(self, side: tk.Frame) -> None:
        self._p_title = tk.Label(side, text="", bg=PANEL, fg=FG, font=(_UI, 12, "bold"),
                                 anchor="w", justify="left")
        self._p_title.pack(fill="x")
        self._p_title.bind("<Configure>", lambda e: self._p_title.config(wraplength=e.width))
        self._btn_enlarge = _Button(side, "🔍  Enlarge  (Space)", self._enlarge)
        self._btn_enlarge.pack(fill="x", pady=(6, 0))

        _caption(side, "TAGS  (click one to remove it)").pack(fill="x", pady=(10, 2))
        self._chips = _Chips(side, self._remove_tag, height=7)
        self._chips.pack(fill="x")

        self._v_add = tk.StringVar()
        row = tk.Frame(side, bg=PANEL)
        row.pack(fill="x", pady=(8, 0))
        self._e_add = _entry(row, self._v_add)
        self._e_add.pack(side="left", fill="x", expand=True, ipady=4)
        _Button(row, "Add", self._add_typed, "primary").pack(side="left", padx=(6, 0))
        self._complete = Autocomplete(self._e_add, self._suggest_names, TYPE_COLOR)
        self._e_add.bind("<Return>", lambda _e: self._add_typed(), add="+")
        tk.Label(side, text="Several at once: dog, wolf, blue_sky", bg=PANEL, fg=FG_DIM,
                 font=FONT_SUB, anchor="w").pack(fill="x")

        self._issues = tk.Label(side, text="", bg=PANEL, fg=_AMBER, font=FONT_SUB, anchor="w",
                                justify="left")
        self._issues.pack(fill="x", pady=(8, 0))
        self._issues.bind("<Configure>", lambda e: self._issues.config(wraplength=e.width))
        self._btn_fix = _Button(side, "Fix tags (synonyms, implications)", self._fix)

        tk.Frame(side, bg=BORDER, height=1).pack(fill="x", pady=12)
        self._ai_caption = _caption(side, "")
        self._ai_caption.pack(fill="x", pady=(0, 2))
        ctl = tk.Frame(side, bg=PANEL)
        ctl.pack(fill="x")
        self._btn_ai = _Button(ctl, "✨  Analyse", self._analyse)
        self._btn_ai.pack(side="left")
        self._btn_all = _Button(ctl, "Accept all", self._accept_all)
        self._btn_all.pack(side="right")
        thr = tk.Frame(side, bg=PANEL)
        thr.pack(fill="x", pady=(6, 0))
        tk.Label(thr, text="Confidence ≥", bg=PANEL, fg=FG_DIM, font=FONT_SUB).pack(side="left")
        self._v_thr = tk.DoubleVar(value=50)
        tk.Scale(thr, from_=20, to=95, orient="horizontal", variable=self._v_thr, showvalue=True,
                 bg=PANEL, fg=FG, troughcolor=ENTRY_BG, highlightthickness=0, bd=0, length=150,
                 command=lambda _v: self._schedule_panel()).pack(side="left", padx=(6, 0))
        self._v_known = tk.BooleanVar(value=False)
        tk.Checkbutton(side, text="Only tags from the Tags page", variable=self._v_known,
                       bg=PANEL, fg=FG, selectcolor=ENTRY_BG, activebackground=PANEL,
                       activeforeground=ACCENT, font=FONT_SUB, anchor="w",
                       command=self._schedule_panel).pack(fill="x")
        self._ai_status = tk.Label(side, text="", bg=PANEL, fg=FG_DIM, font=FONT_SUB,
                                   anchor="w", justify="left")
        self._ai_status.pack(fill="x", pady=(4, 2))
        self._ai_status.bind("<Configure>", lambda e: self._ai_status.config(wraplength=e.width))
        self._props = _Chips(side, self._accept_one, height=8)
        self._props.pack(fill="x")
        self._proposals: dict[str, td.Proposal] = {}

        foot = tk.Frame(side, bg=PANEL)
        foot.pack(side="bottom", fill="x", pady=(8, 0))
        self._btn_undo = _Button(foot, "↶  Undo", self._do_undo)
        self._btn_undo.pack(side="left")
        self._undo_lbl = tk.Label(foot, text="", bg=PANEL, fg=FG_DIM, font=FONT_SUB, anchor="w")
        self._undo_lbl.pack(side="left", padx=8)

    # ══ Screens and keys ══════════════════════════════════════════════════════

    def _show(self, name: str) -> None:
        self._screen = name
        self._grid.pack_forget()
        self._viewer.pack_forget()
        (self._grid if name == "grid" else self._viewer).pack(fill="both", expand=True)

    def handle_back(self) -> bool:
        if self.winfo_ismapped() and self._screen == "viewer":
            self._to_grid()
            return True
        return False

    def _typing(self) -> bool:
        w = self.focus_get()
        return isinstance(w, (tk.Entry, tk.Text))

    def on_key(self, name: str) -> None:
        if not self.winfo_ismapped():
            return
        if name == "undo":
            if not isinstance(self.focus_get(), tk.Text):
                self._do_undo()
            return
        if self._typing():
            return
        if name == "all":
            self._select_all()
        elif name == "escape":
            if self._screen == "viewer":
                self._to_grid()
            else:
                self._changed_cells(self._sel.clear())
                self._schedule_panel()
        elif name in ("enter", "enlarge"):
            if self._screen == "grid":
                self._enlarge()
        elif name in ("left", "right", "up", "down"):
            self._arrow(name)
        elif self._screen == "viewer":
            if name in ("prev", "next"):
                self._step(-1 if name == "prev" else 1)
            elif name in ("first", "last"):
                self._step(-len(self._order) if name == "first" else len(self._order))
            elif name == "fit":
                self._fit()
            elif name == "actual":
                self._actual()
            elif name in ("zoomin", "zoomout"):
                self._zoom(1.4 if name == "zoomin" else 1 / 1.4)

    def _arrow(self, name: str) -> None:
        if not self._view:
            return
        step = {"left": -1, "right": 1, "up": -self._per_row, "down": self._per_row}[name]
        if self._screen == "viewer":
            if name in ("left", "right"):
                self._step(step)
            return
        cur = self._sel.ordered()[-1] if len(self._sel) else (0 if step > 0 else len(self._view) - 1)
        nxt = max(0, min(len(self._view) - 1, cur + step if len(self._sel) else cur))
        self._changed_cells(self._sel.click(nxt))
        self._grid.see(nxt // self._per_row)
        self._schedule_panel()

    # ══ Loading and filtering ═════════════════════════════════════════════════

    def _browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self._v_folder.get() or str(self._base))
        if d:
            self._v_folder.set(d)

    def _load(self) -> None:
        if self._loading is not None:
            return
        folder = Path(self._v_folder.get().strip() or str(self._base))
        if not folder.is_dir():
            self._say(f"Folder not found: {folder}", error=True)
            return
        self._say("Reading the folder…")
        self._loading = queue.Queue()
        out = self._loading

        def work() -> None:
            try:
                paths = td.scan_folder(folder)
                out.put((paths, td.load_tags(self._db, self._base, paths), None))
            except Exception as e:
                out.put(([], {}, str(e)))
        threading.Thread(target=work, name="tagger-load", daemon=True).start()

    def _loaded(self, paths: list[Path], tags: dict[Path, list[str]], error: str | None) -> None:
        self._loading = None
        if error:
            self._say(f"Could not read the folder: {error}", error=True)
            return
        self._paths, self._tags = paths, tags
        self._undo.clear()
        self._sel.clear()
        self._scores.clear()
        self._canon.clear()
        self._closure.clear()
        self._apply_filter(reset=True)
        self._say(f"{len(paths)} image(s) loaded." if paths else "No images in that folder.")

    def _set_filter(self, key: str) -> None:
        self._v_filter.set(key)
        self._style_filters()
        self._apply_filter(reset=True)

    def _style_filters(self) -> None:
        for key, b in self._filter_btns.items():
            on = key == self._v_filter.get()
            b.config(bg=ACCENT if on else ENTRY_BG, fg="#ffffff" if on else FG)

    def _query_changed(self) -> None:
        if getattr(self, "_q_job", None):
            self.after_cancel(self._q_job)
        self._q_job = self.after(250, lambda: self._apply_filter(reset=True))

    def _recheck(self) -> None:
        """Forget what was learned about the Tags page (after editing it) and re-filter."""
        self._canon.clear()
        self._closure.clear()
        self._apply_filter()
        self._schedule_panel()

    def _apply_filter(self, reset: bool = False) -> None:
        mode = self._v_filter.get()
        inc, exc = td.parse_query(self._v_query.get())
        keep: list[Path] = []
        for p in self._paths:
            tags = self._tags.get(p, [])
            if mode == "untagged" and tags:
                continue
            if mode == "tagged" and not tags:
                continue
            if mode == "attention" and not self._issues_of(tags):
                continue
            if (inc or exc) and not td.matches(tags, inc, exc):
                continue
            keep.append(p)
        selected = {self._view[i] for i in self._sel.items if i < len(self._view)}
        self._view = keep
        self._index = {p: i for i, p in enumerate(keep)}
        self._sel.clear()
        for p in selected:
            if p in self._index:
                self._sel.items.add(self._index[p])
        self._relayout(reset)
        self._update_count()
        self._schedule_panel()

    # ── Tags page knowledge (cached) ──────────────────────────────────────────

    def _canonical(self, tag: str) -> str | None:
        if tag not in self._canon:
            found = self._store.find(tag)
            self._canon[tag] = found["name"] if found else None
        return self._canon[tag]

    def _implied(self, canonical: str) -> list[str]:
        if canonical not in self._closure:
            self._closure[canonical] = self._store.implication_closure(canonical)
        return self._closure[canonical]

    def _issues_of(self, tags: list[str]) -> list[str]:
        out: list[str] = []
        have = set(tags)
        for t in tags:
            c = self._canonical(t)
            if c is None:
                out.append(f"{t} is not on the Tags page")
            elif c != t:
                out.append(f"{t} is a synonym of {c}")
            else:
                out += [f"{i} is implied by {t}" for i in self._implied(c)
                        if i not in have and f"{i} is implied by {t}" not in out]
        return out

    # ══ Grid ══════════════════════════════════════════════════════════════════

    def _relayout(self, reset: bool = False) -> None:
        width = max(self._grid.canvas.winfo_width(), _CELL)
        self._per_row = max(1, (width - 8) // _CELL)
        rows = -(-len(self._view) // self._per_row)
        self._grid.set_count(rows, reset=reset)

    def _on_resize(self) -> None:
        width = max(self._grid.canvas.winfo_width(), _CELL)
        per = max(1, (width - 8) // _CELL)
        if per != self._per_row:
            self._relayout()

    def _photo(self, path: Path):
        ph = self._photos.get(path)
        if ph is not None:
            self._photos.move_to_end(path)
            return ph
        jpg = self._jpgs.get(path)
        if jpg:
            try:
                with Image.open(BytesIO(jpg)) as im:
                    ph = ImageTk.PhotoImage(im.copy())
            except Exception:
                self._jpgs[path] = None
                return None
            self._photos[path] = ph
            while len(self._photos) > _PHOTO_CACHE:
                self._photos.popitem(last=False)
            return ph
        if path not in self._jpgs and self._loader is not None:
            self._loader.request(path)
        return None

    def _draw_row(self, c: tk.Canvas, row: int, y: int, width: int, tag: str) -> None:
        first = row * self._per_row
        for col in range(self._per_row):
            i = first + col
            if i >= len(self._view):
                break
            path = self._view[i]
            x = 4 + col * _CELL
            on = i in self._sel
            c.create_rectangle(x + 2, y + 2, x + _CELL - 2, y + _CELL - 2,
                               fill=SEL_BG if on else ENTRY_BG, outline=ACCENT if on else BORDER,
                               width=3 if on else 1, tags=(tag,))
            ph = self._photo(path)
            cx, cy = x + _CELL // 2, y + _CELL // 2 - 6
            if ph is not None:
                c.create_image(cx, cy, image=ph, tags=(tag,))
            else:
                c.create_text(cx, cy, text="…" if path not in self._jpgs else "?", fill=FG_DIM,
                              font=(_UI, 16), tags=(tag,))
            tags = self._tags.get(path, [])
            label, color = (f"{len(tags)} tags", COLOR_OK) if tags else ("untagged", _AMBER)
            c.create_text(x + 10, y + _CELL - 12, text=label, fill=color, anchor="w",
                          font=(_UI, 8), tags=(tag,))
            if tags and self._issues_of(tags):
                c.create_text(x + _CELL - 10, y + _CELL - 12, text="⚠", fill=_AMBER, anchor="e",
                              font=(_UI, 9), tags=(tag,))

    def _item_at(self, row: int, x: int) -> int | None:
        col = (x - 4) // _CELL
        i = row * self._per_row + col
        return i if 0 <= col < self._per_row and 0 <= i < len(self._view) else None

    def _grid_click(self, row: int, x: int) -> None:
        i = self._item_at(row, x)
        if i is None:
            return
        state = self._grid.last_state
        self._changed_cells(self._sel.click(i, ctrl=bool(state & 0x4), shift=bool(state & 0x1)))
        self._schedule_panel()
        self._update_count()

    def _grid_double(self, row: int, x: int) -> None:
        i = self._item_at(row, x)
        if i is not None:
            self._open(self._view[i])

    def _select_all(self) -> None:
        self._changed_cells(self._sel.select_all(len(self._view)))
        self._schedule_panel()
        self._update_count()

    def _changed_cells(self, idxs: set[int]) -> None:
        for r in {i // self._per_row for i in idxs}:
            self._grid.redraw_row(r)
        self._update_count()

    def _update_count(self) -> None:
        n, m = len(self._view), len(self._sel)
        self._count.config(text=(f"{n} images" + (f" · {m} selected" if m else ""))
                           if self._paths else "")

    # ══ Viewer ════════════════════════════════════════════════════════════════

    def _enlarge(self) -> None:
        """Open the selection big. Several selected: step through those only."""
        sel = self._selected_paths()
        if not sel:
            self._say("Select an image first.")
            return
        self._order = sel if len(sel) > 1 else self._view
        self._focus(sel[0])
        self._show("viewer")

    def _open(self, path: Path) -> None:
        """Double-click: select this image alone and open it, stepping through the grid."""
        i = self._index.get(path)
        if i is None:
            return
        self._changed_cells(self._sel.click(i))
        self._order = self._view
        self._focus(path)
        self._show("viewer")
        self._schedule_panel()

    def _focus(self, path: Path) -> None:
        self._viewing = path
        self._vview.reset()
        self._pane.show(str(path))
        n = len(self._order)
        pos = self._order.index(path) + 1 if path in self._order else 0
        scope = "selection" if self._order is not self._view and n != len(self._view) else "all"
        self._v_name.config(text=f"{path.name}   ·   {pos} / {n}"
                                 + ("  (selection)" if scope == "selection" else ""))
        self._update_viewer_tags()

    def _update_viewer_tags(self) -> None:
        tags = self._tags.get(self._viewing, []) if self._viewing else []
        self._v_tags.config(text=("Tags: " + ", ".join(tags)) if tags else "No tags yet.")

    def _to_grid(self) -> None:
        self._show("grid")
        if self._viewing in self._index:
            self._grid.see(self._index[self._viewing] // self._per_row)

    def _step(self, d: int) -> None:
        """Previous / next image. Over the grid it moves the selection with it."""
        if self._viewing not in self._order:
            return
        j = max(0, min(len(self._order) - 1, self._order.index(self._viewing) + d))
        path = self._order[j]
        if self._order is self._view:                 # follow in the grid, tag this image
            i = self._index[path]
            self._changed_cells(self._sel.click(i))
            self._schedule_panel()
        self._focus(path)

    def _zoom(self, factor: float) -> None:
        self._vview.zoom_by(factor)
        self._pane.request_redraw()

    def _fit(self) -> None:
        self._vview.reset()
        self._pane.request_redraw()

    def _actual(self) -> None:
        im = self._pane.image
        if im is None:
            return
        bw, bh = self._pane.size()
        self._vview.zoom = 1.0 / fit_scale(im.width, im.height, bw, bh)
        self._vview.cx = self._vview.cy = 0.5
        self._vview.clamp()
        self._pane.request_redraw()

    # ══ Panel ═════════════════════════════════════════════════════════════════

    def _selected_paths(self) -> list[Path]:
        return [self._view[i] for i in self._sel.ordered() if i < len(self._view)]

    def _schedule_panel(self) -> None:
        if self._panel_job is None:
            self._panel_job = self.after(60, self._refresh_panel)

    def _refresh_panel(self) -> None:
        self._panel_job = None
        paths = self._selected_paths()
        n = len(paths)
        if n == 0:
            self._p_title.config(text="Nothing selected")
            self._chips.set([], "Click an image to see its tags.\nCtrl+click or Shift+click for several.")
            self._issues.config(text="")
            self._btn_fix.pack_forget()
            self._set_proposals([], 0)
            return
        self._p_title.config(text=paths[0].name if n == 1 else f"{n} images selected")
        lists = [self._tags.get(p, []) for p in paths]
        chips = []
        for tag, k in td.common_tags(lists):
            c = self._canonical(tag)
            color = TYPE_COLOR.get((self._store.find(tag) or {}).get("category", ""), FG) if c else _AMBER
            chips.append((tag if k == n else f"{tag}  {k}/{n}", tag, color if k == n else FG_DIM))
        self._chips.set(chips, "No tags yet.")
        issues: list[str] = []
        for tags in lists:
            for msg in self._issues_of(tags):
                if msg not in issues:
                    issues.append(msg)
        if issues:
            self._issues.config(text="⚠ " + "; ".join(issues[:3]) + (f"; +{len(issues) - 3} more" if len(issues) > 3 else ""))
            self._btn_fix.pack(fill="x", pady=(4, 0), after=self._issues)
        else:
            self._issues.config(text="")
            self._btn_fix.pack_forget()
        self._refresh_ai(paths)
        if self._screen == "viewer":
            self._update_viewer_tags()
        self._btn_undo.set_enabled(bool(self._undo))
        self._undo_lbl.config(text=self._undo[-1][0] if self._undo else "")

    def _suggest_names(self, prefix: str, others: list[str]) -> list[dict]:
        paths = self._selected_paths()
        have = set(others)
        if len(paths) == 1:
            have |= set(self._tags.get(paths[0], []))
        return self._store.suggest(prefix, 8, list(have))

    # ── AI ────────────────────────────────────────────────────────────────────

    def _refresh_ai(self, paths: list[Path]) -> None:
        sample = paths[:_MAX_AGG]
        for p in sample:
            if p not in self._scores:
                got = self._ai_cache.get(p)
                if got is not None:
                    self._scores[p] = got
        analysed = [p for p in sample if p in self._scores]
        spec = self._spec
        installed = ai_tagger.installed(spec)
        self._ai_caption.config(text=f"AI SUGGESTIONS · {spec.title}  (click one to accept)")
        if self._ai.busy:
            pass
        elif not installed:
            if spec.base_url:
                missing = f"The model ({spec.size_mb} MB) is not downloaded yet. Press Download, or pick another in ⚙ settings."
            else:
                missing = f"Model files not found in {ai_tagger.model_dir(spec)}. Check ⚙ settings."
            self._ai_status.config(text=self._download_msg or missing, fg=FG_DIM)
        elif not analysed:
            self._ai_status.config(text="Press Analyse to get suggestions for the selection.", fg=FG_DIM)
        elif len(analysed) < len(sample):
            self._ai_status.config(text=f"{len(analysed)} of {len(sample)} images analysed.", fg=FG_DIM)
        else:
            self._ai_status.config(text="", fg=FG_DIM)
        self._btn_ai.config(text="⬇  Download model" if (not installed and spec.base_url)
                            else "✨  Analyse")
        have = {p: self._tags.get(p, []) for p in analysed}
        props = td.aggregate({p: self._scores[p] for p in analysed}, have, self._store,
                             self._v_thr.get() / 100.0, self._v_known.get())
        self._set_proposals(props, len(sample))
        if len(paths) > _MAX_AGG:
            self._ai_status.config(text=f"Suggestions use the first {_MAX_AGG} images.", fg=FG_DIM)

    def _set_proposals(self, props: list[td.Proposal], total: int) -> None:
        self._proposals = {p.tag: p for p in props}
        chips = []
        for p in props:
            label = f"{p.tag}  {int(p.score * 100)}%" if total <= 1 else f"{p.tag}  {len(p.paths)}/{total}"
            chips.append((label, p.tag, FG if p.known else _AMBER))
        self._props.set(chips, "")
        self._btn_all.set_enabled(bool(props))

    def _analyse(self) -> None:
        if self._ai.busy:
            return
        if not ai_tagger.installed(self._spec):
            if not self._spec.base_url:
                self._ai_status.config(text="The custom model files are missing. Check ⚙ settings.",
                                       fg=COLOR_FAIL)
            elif self._ai.download():
                self._ai_status.config(text="Downloading the model…", fg=FG_DIM)
            return
        paths = self._selected_paths()
        if not paths:
            self._say("Select images first.")
            return
        if self._ai.score(paths):
            self._ai_status.config(text="Loading the model…", fg=FG_DIM)

    def _accept_one(self, tag: str) -> None:
        prop = self._proposals.get(tag)
        if prop is not None:
            self._edit(list(prop.paths), add=[tag], label=f"added {tag}")

    def _accept_all(self) -> None:
        if not self._proposals:
            return
        group: list[tagging.TagEdit] = []
        for tag, prop in self._proposals.items():
            group += self._write(list(prop.paths), add=[tag])
        self._finish_edit(group, f"accepted {len(self._proposals)} suggestions")

    # ══ Editing ═══════════════════════════════════════════════════════════════

    def _write(self, paths: list[Path], add: list[str] = (), remove: list[str] = ()
               ) -> list[tagging.TagEdit]:
        edits = tagging.edit_many(self._db, self._base, paths, list(add), list(remove),
                                  self._store)
        for e in edits:
            self._tags[e.path] = e.after
        return edits

    def _finish_edit(self, edits: list[tagging.TagEdit], label: str) -> None:
        if not edits:
            self._say("Nothing to change.")
            return
        n = len({e.path for e in edits})
        self._undo.append((f"{label} ({n} image{'s' if n != 1 else ''})", edits))
        self._undo = self._undo[-50:]
        self._after_change({e.path for e in edits}, f"{label}: {n} image(s) changed.")

    def _edit(self, paths: list[Path], add=(), remove=(), label: str = "edited") -> None:
        self._finish_edit(self._write(paths, add, remove), label)

    def _after_change(self, changed: set[Path], message: str) -> None:
        if self._v_filter.get() != "all" or self._v_query.get().strip():
            self._apply_filter()                    # rows may enter or leave the filter
        else:
            self._changed_cells({self._index[p] for p in changed if p in self._index})
        self._schedule_panel()
        self._say(message)

    def _add_typed(self) -> None:
        tags = [t for t in self._v_add.get().replace("\n", ",").split(",") if t.strip()]
        paths = self._selected_paths()
        if not tags or not paths:
            return
        self._v_add.set("")
        res = tagging.resolve(tags, self._store, False)
        before = len(self._undo)
        self._edit(paths, add=tags, label="added " + ", ".join(res.tags[:3]))
        unknown = res.unknown
        if unknown and len(self._undo) > before:
            self._say(f"Saved. '{', '.join(unknown)}' is not on the Tags page yet.")
        self._complete.hide()

    def _remove_tag(self, tag: str) -> None:
        paths = [p for p in self._selected_paths() if tag in self._tags.get(p, [])]
        if paths:
            self._edit(paths, remove=[tag], label=f"removed {tag}")

    def _fix(self) -> None:
        edits = tagging.fix_many(self._db, self._base, self._selected_paths(), self._store)
        for e in edits:
            self._tags[e.path] = e.after
        self._finish_edit(edits, "fixed tags")

    def _do_undo(self) -> None:
        if not self._undo:
            return
        label, edits = self._undo.pop()
        tagging.undo_edits(self._db, self._base, list(reversed(edits)))
        for e in edits:
            self._tags[e.path] = tagging.current_tags(self._db, self._base, e.path)
        self._after_change({e.path for e in edits}, f"Undone: {label}.")

    # ══ Background results ════════════════════════════════════════════════════

    def _say(self, text: str, error: bool = False) -> None:
        self._status.config(text=text, fg=COLOR_FAIL if error else FG_DIM)

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        if self._loading is not None:
            try:
                self._loaded(*self._loading.get_nowait())
            except queue.Empty:
                pass
        self._ticks += 1
        if self._ticks % 8 == 0 and not self._ai.busy:           # ⚙ settings: about 1 s
            cfg = tagger_settings.load()
            if cfg.signature() != self._model_sig:
                self._use_model(cfg)
                self._say(f"AI model: {self._spec.title}.")
                self._schedule_panel()
        if self._loader is not None:
            for path, jpg in self._loader.poll():
                self._jpgs[path] = jpg
                i = self._index.get(path)
                if i is not None:
                    self._grid.redraw_row(i // self._per_row)
        for ev in self._ai.poll():
            if ev.kind == "download":
                mb = ev.done / 1e6
                tot = f" / {ev.total / 1e6:.0f}" if ev.total else ""
                self._ai_status.config(text=f"Downloading {ev.message}: {mb:.0f}{tot} MB", fg=FG_DIM)
            elif ev.kind == "model":
                self._download_msg = ""
                self._say("AI model ready. Select images and press Analyse.")
                self._schedule_panel()
            elif ev.kind == "progress":
                self._ai_status.config(text=f"Analysing {ev.done}/{ev.total}…", fg=FG_DIM)
            elif ev.kind == "scored":
                self._say(f"Analysed {ev.done} image(s).")
                self._schedule_panel()
            elif ev.kind == "error":
                self._ai_status.config(text=ev.message, fg=COLOR_FAIL)
                self._download_msg = ev.message
        self.after(_POLL_MS, self._poll)


def build_tagger(parent: tk.Frame) -> None:
    db = get_db()
    if db is None:
        tk.Label(parent, text="Database not available.", bg=PANEL, fg=COLOR_FAIL).pack(pady=40)
        return
    path = str(getattr(db, "_path", "")) or None
    TaggerPage(parent, db, TagStore(db.connection), path).pack(fill="both", expand=True)
