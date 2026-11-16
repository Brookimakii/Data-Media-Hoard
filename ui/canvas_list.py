"""
ui/canvas_list.py
-----------------
A long list with real, native scrolling.

Everything is drawn on one Canvas, and scrolling is the canvas's own (yview): moving the
wheel or the scrollbar costs no Python work per frame. Only rows within a few screens of
the viewport exist as canvas items; rows are drawn ahead of the viewport and removed when
far away, so thousands of rows stay cheap and nothing pops in while you scroll.

    draw_row(canvas, index, y, width, tag)   draw item `index` at top coordinate y; every
                                             canvas item must carry `tag`
    on_click(index, x)                       click on a row (x relative to the row)
    on_double(index, x)                      double-click on a row
"""

from __future__ import annotations

import time
import tkinter as tk
from typing import Callable

from core.virtual_list import visible_rows

_STEP_PX = 24            # one scroll "unit"
_WHEEL_UNITS = 3         # units per wheel notch (72 px)
_AHEAD = 1.5             # screens drawn above and below the viewport


class CanvasList(tk.Frame):
    def __init__(self, parent: tk.Misc, row_h: int,
                 draw_row: Callable[[tk.Canvas, int, int, int, str], None],
                 on_click: Callable[[int, int], None], on_double: Callable[[int, int], None],
                 bg: str) -> None:
        super().__init__(parent, bg=bg)
        self._row_h, self._draw = row_h, draw_row
        self._on_click, self._on_double = on_click, on_double
        self._count = 0
        self._drawn: set[int] = set()
        self._width = 0
        self._job = None
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, yscrollincrement=_STEP_PX)
        self._sb = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview, bg=bg,
                                troughcolor=bg, bd=0, highlightthickness=0, width=12)
        self.canvas.configure(yscrollcommand=self._on_scrolled)
        self._sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        c = self.canvas
        c.bind("<Configure>", self._on_configure)
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-4>", self._wheel)
        c.bind("<Button-5>", self._wheel)
        c.bind("<Button-1>", lambda e: self._clicked(e, self._on_click))
        c.bind("<Double-Button-1>", lambda e: self._clicked(e, self._on_double))

    # ── public ────────────────────────────────────────────────────────────────
    @property
    def count(self) -> int:
        return self._count

    def set_count(self, n: int, reset: bool = False) -> None:
        """New list of n items (everything is redrawn)."""
        self._count = n
        self._set_region()
        if reset:
            self.canvas.yview_moveto(0)
        self.refresh()

    def refresh(self) -> None:
        """Redraw every row that currently exists (data changed, same count)."""
        self.canvas.delete("all")
        self._drawn.clear()
        self._sync()

    def redraw_row(self, index: int) -> None:
        if index in self._drawn:
            self.canvas.delete(f"r{index}")
            self._drawn.discard(index)
            self._draw_one(index)

    def see(self, index: int) -> None:
        top, bottom = index * self._row_h, (index + 1) * self._row_h
        view_top, view_h = self._top(), self._view_h()
        if top < view_top:
            self._moveto(top)
        elif bottom > view_top + view_h:
            self._moveto(bottom - view_h)

    def visible(self) -> range:
        return visible_rows(int(self._top()), self._count, self._row_h, self._view_h())

    # ── internals ─────────────────────────────────────────────────────────────
    def _view_h(self) -> int:
        return max(1, self.canvas.winfo_height())

    def _top(self) -> float:
        return float(self.canvas.canvasy(0))

    def _total(self) -> int:
        return max(self._count * self._row_h, self._view_h())

    def _set_region(self) -> None:
        self.canvas.configure(scrollregion=(0, 0, max(1, self._width), self._total()))

    def _moveto(self, y: float) -> None:
        total = self._total()
        self.canvas.yview_moveto(max(0.0, min(y, total - self._view_h())) / total)

    def _on_configure(self, e: tk.Event) -> None:
        if e.width != self._width:
            self._width = e.width
            self.refresh()                       # rows span the full width
        self._set_region()
        self._schedule()

    def _on_scrolled(self, first: str, last: str) -> None:
        self._sb.set(first, last)
        self._schedule()

    def _schedule(self) -> None:
        """Draw ahead of the viewport once per ~10 ms, however fast the scrolling is."""
        if self._job is None:
            self._job = self.after(10, self._flush)

    def _flush(self) -> None:
        self._job = None
        self._sync()

    def _sync(self) -> None:
        if self._count <= 0:
            self.canvas.delete("all")
            self._drawn.clear()
            return
        view_h, top = self._view_h(), int(self._top())
        pad = int(view_h * _AHEAD)
        want = set(visible_rows(max(0, top - pad), self._count, self._row_h, view_h + 2 * pad))
        for i in self._drawn - want:
            self.canvas.delete(f"r{i}")
        self._drawn &= want
        # nearest to the viewport first, so what you see appears first
        middle = (top + view_h / 2) / self._row_h
        shown = set(visible_rows(top, self._count, self._row_h, view_h))
        started = time.perf_counter()
        for i in sorted(want - self._drawn, key=lambda r: abs(r - middle)):
            if i not in shown and time.perf_counter() - started > 0.008:
                self._schedule()                 # off-screen rows wait for the next frame
                break
            self._draw_one(i)

    def _draw_one(self, i: int) -> None:
        self._drawn.add(i)
        self._draw(self.canvas, i, i * self._row_h, max(1, self._width), f"r{i}")

    def _wheel(self, e: tk.Event) -> None:
        up = getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0
        self.canvas.yview_scroll(-_WHEEL_UNITS if up else _WHEEL_UNITS, "units")

    def _clicked(self, e: tk.Event, handler: Callable[[int, int], None]) -> None:
        y = self.canvas.canvasy(e.y)
        i = int(y // self._row_h)
        if 0 <= i < self._count:
            handler(i, int(self.canvas.canvasx(e.x)))
