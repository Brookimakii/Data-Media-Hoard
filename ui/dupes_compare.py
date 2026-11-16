"""
ui/dupes_compare.py
-------------------
The detail view of one duplicate group: two files side by side with the metadata in
the middle.

  * Wheel zooms around the cursor, drag pans. Both images follow each other and stay
    aligned even at different resolutions (core.dupes_meta.View). "Sync" lets you
    move one alone.
  * Pick any two files of the group with the Left / Right menus (← → change the right
    one, Shift+← → the left one). Space flips the two images in place.
  * Middle column: shared folder once, then one row per property, green = same,
    orange = different (path, date, size, resolution, artist, format, aspect, bytes).
  * Under each image: what to do with that file
        ✓ Keep        it will be uploaded
        ✕ Throw away  it will not be uploaded (stays on disk)
        ✎ It's an edit  different work, not a duplicate: uploaded, split off the group
No scrollbars.
"""

from __future__ import annotations

import logging
import math
import tkinter as tk
from pathlib import Path
from typing import Callable

from PIL import Image, ImageTk

from core.dupes import Entry
from core.dupes_meta import View, compare_rows, cursor_offset, fit_scale, view_box
from core.dupes_review import EDIT, KEEP, THROW
from ui.theme import (
    BG, BTN_BG, BTN_FG, BTN_HOV, COLOR_FAIL, COLOR_OK, ENTRY_BG, FG, FG_DIM,
    FONT_BTN, FONT_MONO, PANEL, SEL_BG,
)

log = logging.getLogger(__name__)

_UI = FONT_BTN[0]
_ORANGE = "#e08a3c"
_BLUE = "#4f8fd9"
STATE_COLOR = {KEEP: COLOR_OK, THROW: COLOR_FAIL, EDIT: _BLUE}
STATE_TEXT = {KEEP: "KEEP", THROW: "THROW", EDIT: "EDIT"}
_SEGMENTS = ((KEEP, "✓ Keep"), (THROW, "✕ Throw away"), (EDIT, "✎ It's an edit"))


class Btn(tk.Label):
    def __init__(self, parent, text, command, kind="ghost") -> None:
        bg, hov, fg = {"primary": (BTN_BG, BTN_HOV, BTN_FG), "ghost": (ENTRY_BG, SEL_BG, FG),
                       "danger": (ENTRY_BG, "#5a2530", COLOR_FAIL)}[kind]
        super().__init__(parent, text=text, bg=bg, fg=fg, font=FONT_BTN, padx=12, pady=5,
                         cursor="hand2")
        self._bg, self._hov = bg, hov
        self.bind("<Button-1>", lambda _e: command())
        self.bind("<Enter>", lambda _e: self.config(bg=self._hov))
        self.bind("<Leave>", lambda _e: self.config(bg=self._bg))


class _Segments(tk.Frame):
    """Three-way choice: Keep / Throw away / It's an edit. The current one is filled."""

    def __init__(self, parent, on_pick: Callable[[str], None]) -> None:
        super().__init__(parent, bg=BG)
        self._btns: dict[str, tk.Label] = {}
        for state, text in _SEGMENTS:
            b = tk.Label(self, text=text, bg=ENTRY_BG, fg=FG, font=FONT_BTN, padx=10, pady=7,
                         cursor="hand2")
            b.pack(side="left", fill="x", expand=True, padx=(0, 4))
            b.bind("<Button-1>", lambda _e, s=state: on_pick(s))
            self._btns[state] = b

    def show(self, current: str) -> None:
        for state, b in self._btns.items():
            on = state == current
            b.config(bg=STATE_COLOR[state] if on else ENTRY_BG, fg="#101014" if on else FG)


class _Pane(tk.Frame):
    """One image view. Draws only the visible region (never decodes at zoomed size)."""

    def __init__(self, parent, view: View, on_changed) -> None:
        super().__init__(parent, bg="#000000")
        self.view = view
        self._on_changed = on_changed
        self.image: Image.Image | None = None
        self._levels: dict[int, Image.Image] = {}     # image halved k times (mip levels)
        self._photo = None
        self._drag: tuple[int, int] | None = None
        self._job = None                              # pending throttled redraw
        self._settle = None                           # pending high-quality redraw
        self.canvas = tk.Canvas(self, bg="#000000", highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        c = self.canvas
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-4>", self._wheel)
        c.bind("<Button-5>", self._wheel)
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._motion)
        c.bind("<Configure>", lambda _e: self.request_redraw())

    def show(self, path: str) -> None:
        try:
            with Image.open(path) as im:
                im.load()
                if "A" in im.getbands():             # flatten transparency: RGB is much faster
                    rgba = im.convert("RGBA")
                    bg = Image.new("RGBA", rgba.size, (30, 30, 36, 255))
                    self.image = Image.alpha_composite(bg, rgba).convert("RGB")
                else:
                    self.image = im.convert("RGB")
        except Exception as e:                      # unreadable / video
            log.debug("compare: cannot open %s: %s", path, e)
            self.image = None
        self._levels = {0: self.image} if self.image is not None else {}
        self.redraw()

    def size(self) -> tuple[int, int]:
        return max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())

    def _level(self, k: int) -> Image.Image:
        """The image halved k times, built once from the previous level."""
        have = max(j for j in self._levels if j <= k)
        img = self._levels[have]
        for j in range(have + 1, k + 1):
            img = img.reduce(2)
            self._levels[j] = img
        return self._levels[k]

    def request_redraw(self) -> None:
        """Smooth interaction: at most one redraw per ~16 ms, drawn fast (nearest
        neighbour); the sharp version follows 160 ms after the last event."""
        if self._job is None:
            self._job = self.after(16, self._flush)

    def _flush(self) -> None:
        self._job = None
        self.redraw(fast=True)
        if self._settle is not None:
            self.after_cancel(self._settle)
        self._settle = self.after(160, self._sharpen)

    def _sharpen(self) -> None:
        self._settle = None
        self.redraw()

    def redraw(self, fast: bool = False) -> None:
        c = self.canvas
        c.delete("all")
        bw, bh = self.size()
        if self.image is None:
            c.create_text(bw // 2, bh // 2, text="No preview", fill=FG_DIM, font=(_UI, 11))
            return
        box = view_box(self.image.width, self.image.height, bw, bh, self.view)
        if box is None:
            return
        src, (x, y, w, h) = box
        s = fit_scale(self.image.width, self.image.height, bw, bh) * self.view.zoom
        k = 0 if s >= 1 else min(8, int(math.log2(1 / s)))     # decode from a smaller level
        im, f = self._level(k), 2 ** k
        sx0, sy0, sx1, sy1 = (v / f for v in src)
        sx1, sy1 = min(sx1, im.width), min(sy1, im.height)
        if sx1 <= sx0 or sy1 <= sy0:
            return
        sharp = not fast and self.view.zoom < 8
        tile = im.resize((w, h), Image.BILINEAR if sharp else Image.NEAREST,
                         box=(sx0, sy0, sx1, sy1))
        self._photo = ImageTk.PhotoImage(tile)
        c.create_image(x, y, image=self._photo, anchor="nw")

    def _wheel(self, e) -> None:
        up = getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0
        if self.image is None:
            return
        bw, bh = self.size()
        u, v = cursor_offset(e.x, e.y, self.image.width, self.image.height, bw, bh, self.view)
        self.view.zoom_by(1.15 if up else 1 / 1.15, u, v)
        self._on_changed(self)

    def _press(self, e) -> None:
        self._drag = (e.x, e.y)

    def _motion(self, e) -> None:
        if self._drag is None or self.image is None:
            return
        dx, dy = e.x - self._drag[0], e.y - self._drag[1]
        self._drag = (e.x, e.y)
        bw, bh = self.size()
        s = fit_scale(self.image.width, self.image.height, bw, bh) * self.view.zoom
        self.view.pan_by(dx / (s * self.image.width), dy / (s * self.image.height))
        self._on_changed(self)


class ComparePanel(tk.Frame):
    """Detail view. The page gives it the group's files and gets decisions back."""

    def __init__(self, parent: tk.Misc, *, on_back: Callable[[], None],
                 on_prev: Callable[[], None], on_next: Callable[[], None],
                 get_state: Callable[[int], str], set_state: Callable[[int, str], None]) -> None:
        super().__init__(parent, bg=BG)
        self._on_back, self._on_prev, self._on_next = on_back, on_prev, on_next
        self._get_state, self._set_state = get_state, set_state
        self._ents: list[Entry] = []
        self._ids: list[int] = []                   # entry index of each member (for the page)
        self._score: Callable[[Entry, Entry], float | None] = lambda a, b: None
        self._scan_root = ""
        self._left = self._right = 0
        self._flicker = False
        self._lock = True
        self.view = View()
        self._build()

    # ── layout ────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        top = tk.Frame(self, bg=PANEL)
        top.pack(fill="x")
        Btn(top, "‹ Groups", lambda: self._on_back()).pack(side="left", padx=10, pady=8)
        self._title = tk.Label(top, text="", bg=PANEL, fg=FG, font=(_UI, 12, "bold"))
        self._title.pack(side="left", padx=6)
        Btn(top, "‹", lambda: self._on_prev()).pack(side="left", padx=(8, 2))
        Btn(top, "›", lambda: self._on_next()).pack(side="left")
        self._lock_btn = Btn(top, "🔗 Sync", self._toggle_lock)
        self._lock_btn.pack(side="right", padx=(4, 10))
        Btn(top, "1:1", self._actual).pack(side="right", padx=4)
        Btn(top, "Fit", self._fit).pack(side="right")
        Btn(top, "⇄ Swap", self._swap).pack(side="right", padx=8)

        grid = tk.Frame(self, bg=BG)
        grid.pack(fill="both", expand=True, padx=8, pady=6)
        grid.columnconfigure(0, weight=5, uniform="side")
        grid.columnconfigure(1, weight=0, minsize=310)
        grid.columnconfigure(2, weight=5, uniform="side")
        grid.rowconfigure(1, weight=1)

        self._menu: dict[str, tk.OptionMenu] = {}
        self._var: dict[str, tk.StringVar] = {}
        self._seg: dict[str, _Segments] = {}
        self._pane: dict[str, _Pane] = {}
        for col, side in ((0, "left"), (2, "right")):
            self._var[side] = tk.StringVar()
            m = tk.OptionMenu(grid, self._var[side], "")
            m.config(bg=ENTRY_BG, fg=FG, activebackground=SEL_BG, activeforeground=FG,
                     highlightthickness=0, relief="flat", font=FONT_MONO, anchor="w")
            m.grid(row=0, column=col, sticky="ew", padx=4, pady=(0, 4))
            self._menu[side] = m
            pane = _Pane(grid, self.view, self._changed)
            pane.grid(row=1, column=col, sticky="nsew", padx=4)
            self._pane[side] = pane
            seg = _Segments(grid, lambda st, s=side: self._pick(s, st))
            seg.grid(row=2, column=col, sticky="ew", padx=4, pady=(6, 0))
            self._seg[side] = seg

        self._meta = tk.Frame(grid, bg=ENTRY_BG)
        self._meta.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=4)
        self._legend = tk.Label(
            grid, bg=BG, fg=FG_DIM, font=(_UI, 8), justify="left", anchor="w", wraplength=300,
            text="Keep: uploaded.  Throw away: not uploaded, stays on disk.\n"
                 "Edit: different work, not a duplicate; uploaded and split off.")
        self._legend.grid(row=2, column=1, sticky="ew", padx=4, pady=(6, 0))

    # ── data ──────────────────────────────────────────────────────────────────
    def show(self, title: str, ents: list[Entry], ids: list[int], left: int, right: int,
             score_of: Callable[[Entry, Entry], float | None], scan_root: str) -> None:
        self._title.config(text=title)
        self._ents, self._ids, self._score, self._scan_root = ents, ids, score_of, scan_root
        self._left, self._right = left, right
        if self._left == self._right and len(ents) > 1:       # never the same file twice
            self._right = (self._left + 1) % len(ents)
        self._flicker = False
        self._fill_menus()
        self._fit()
        self._load()

    def _label(self, i: int) -> str:
        return f"{i + 1}. {Path(self._ents[i].path).name}"

    def _fill_menus(self) -> None:
        for side, m in self._menu.items():
            menu = m["menu"]
            menu.delete(0, "end")
            for i in range(len(self._ents)):
                menu.add_command(label=self._label(i), command=lambda i=i, s=side: self._choose(s, i))

    def _choose(self, side: str, i: int) -> None:
        """Put file `i` on `side`. If it is already on the other side, the two swap."""
        if side == "left":
            if i == self._right:
                self._right = self._left
            self._left = i
        else:
            if i == self._left:
                self._left = self._right
            self._right = i
        self._load()

    def step(self, side: str, d: int) -> None:
        """Next/previous file on `side`, skipping the one shown on the other side."""
        n = len(self._ents)
        if n < 2:
            return
        cur = self._left if side == "left" else self._right
        other = self._right if side == "left" else self._left
        i = (cur + d) % n
        while i == other:
            i = (i + d) % n
        self._choose(side, i)

    def _swap(self) -> None:
        self._left, self._right = self._right, self._left
        self._load()

    def _load(self) -> None:
        if not self._ents:
            return
        a, b = self._ents[self._left], self._ents[self._right]
        self._var["left"].set(self._label(self._left))
        self._var["right"].set(self._label(self._right))
        shown = (b, a) if self._flicker else (a, b)
        self._pane["left"].show(shown[0].path)
        self._pane["right"].show(shown[1].path)
        self.refresh()

    def refresh(self) -> None:
        """Re-read decisions and metadata (after a mark changed)."""
        if not self._ents:
            return
        a, b = self._ents[self._left], self._ents[self._right]
        self._seg["left"].show(self._get_state(self._ids[self._left]))
        self._seg["right"].show(self._get_state(self._ids[self._right]))
        root, rows = compare_rows(a, b, self._scan_root, self._score(a, b))
        for w in self._meta.winfo_children():
            w.destroy()
        self._meta.columnconfigure(0, weight=1, uniform="v")
        self._meta.columnconfigure(1, weight=1, uniform="v")
        r = 0
        if root:
            tk.Label(self._meta, text=f"Folder  {root}", bg=ENTRY_BG, fg=FG_DIM, font=FONT_MONO,
                     wraplength=290, justify="left", anchor="w"
                     ).grid(row=r, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 4))
            r += 1
        for row in rows:
            color = FG if row.same is None else (COLOR_OK if row.same else _ORANGE)
            tk.Label(self._meta, text=row.label.upper(), bg=ENTRY_BG, fg=FG_DIM, font=FONT_MONO,
                     anchor="w").grid(row=r, column=0, columnspan=2, sticky="ew", padx=10,
                                      pady=(8, 0))
            for col, txt in ((0, row.left), (1, row.right)):
                tk.Label(self._meta, text=txt or " ", bg=ENTRY_BG, fg=color, font=FONT_MONO,
                         wraplength=140, justify="left", anchor="w"
                         ).grid(row=r + 1, column=col, sticky="ew", padx=(10, 2))
            r += 2

    def _pick(self, side: str, state: str) -> None:
        self._set_state(self._ids[self._left if side == "left" else self._right], state)
        self.refresh()

    # ── view sync ─────────────────────────────────────────────────────────────
    def _changed(self, pane: _Pane) -> None:
        if self._lock:                      # one shared view: both panes follow
            self._redraw()
        else:
            pane.request_redraw()

    def _redraw(self) -> None:
        self._pane["left"].request_redraw()
        self._pane["right"].request_redraw()

    def _fit(self) -> None:
        for p in self._pane.values():
            p.view.reset()
        self._redraw()

    def _actual(self) -> None:
        img = self._pane["left"].image
        if img is None:
            return
        bw, bh = self._pane["left"].size()
        zoom = 1 / fit_scale(img.width, img.height, bw, bh)
        for p in self._pane.values():
            p.view.zoom = zoom
            p.view.clamp()
        self._redraw()

    def _toggle_lock(self) -> None:
        """Sync on: both panes share one View. Off: the right pane gets its own copy."""
        self._lock = not self._lock
        left = self._pane["left"].view
        self._pane["right"].view = left if self._lock else View(left.zoom, left.cx, left.cy)
        self._lock_btn.config(text="🔗 Sync" if self._lock else "⛓ Free")
        if self._lock:
            self._redraw()

    def flicker(self) -> None:
        self._flicker = not self._flicker
        self._load()

    def fit(self) -> None:
        self._fit()

    def actual(self) -> None:
        self._actual()