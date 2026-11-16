"""
ui/recycle_list.py
------------------
A recycling list: only as many row widgets as fit the viewport exist, and they are
re-filled while scrolling (core.virtual_list does the arithmetic). Event-driven: no
timers, nothing runs while idle, so a list of thousands of rows costs the same as ten.

    make_row(parent) -> widget        build one empty row (called a handful of times)
    bind_row(widget, index)           fill that row with item `index`
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

from core.virtual_list import (
    clamp_offset, offset_to_show, pool_size, scrollbar_fractions, visible_rows,
)

_WHEEL_STEP = 70


class RecycleList(tk.Frame):
    def __init__(self, parent: tk.Misc, row_h: int, make_row: Callable[[tk.Misc], tk.Misc],
                 bind_row: Callable[[tk.Misc, int], None], bg: str) -> None:
        super().__init__(parent, bg=bg)
        self._row_h, self._make, self._bind = row_h, make_row, bind_row
        self._count = 0
        self._offset = 0
        self._rows: list[tk.Misc] = []
        self._slot: list[int | None] = []
        self._job = None                              # pending coalesced layout
        self._sb = tk.Scrollbar(self, orient="vertical", command=self._yview, bg=bg,
                                troughcolor=bg, bd=0, highlightthickness=0, width=12)
        self._sb.pack(side="right", fill="y")
        self.body = tk.Frame(self, bg=bg)
        self.body.pack(side="left", fill="both", expand=True)
        self.body.bind("<Configure>", lambda _e: self._layout())
        self.bind_all("<MouseWheel>", self._wheel, add="+")
        self.bind_all("<Button-4>", self._wheel, add="+")
        self.bind_all("<Button-5>", self._wheel, add="+")

    # ── public ────────────────────────────────────────────────────────────────
    @property
    def count(self) -> int:
        return self._count

    def set_count(self, n: int, reset: bool = False) -> None:
        self._count = n
        self._offset = 0 if reset else self._offset
        self._layout(force=True)

    def refresh(self) -> None:
        """Re-fill the visible rows (data changed, same count)."""
        self._layout(force=True)

    def bound_rows(self) -> list[tuple[int, tk.Misc]]:
        """(item index, row widget) of every row currently showing an item."""
        return [(i, w) for i, w in zip(self._slot, self._rows) if i is not None]

    def see(self, index: int) -> None:
        self._offset = offset_to_show(index, self._offset, self._row_h, self._view_h())
        self._layout()

    # ── internals ─────────────────────────────────────────────────────────────
    def _view_h(self) -> int:
        return max(1, self.body.winfo_height())

    def _layout(self, force: bool = False) -> None:
        view_h = self._view_h()
        self._offset = clamp_offset(self._offset, self._count, self._row_h, view_h)
        need = pool_size(view_h, self._row_h)
        while len(self._rows) < need:
            self._rows.append(self._make(self.body))
            self._slot.append(None)
        pool = len(self._rows)
        shown = set()
        for i in visible_rows(self._offset, self._count, self._row_h, view_h):
            s = i % pool
            shown.add(s)
            if force or self._slot[s] != i:
                self._bind(self._rows[s], i)
                self._slot[s] = i
            self._rows[s].place(x=0, y=i * self._row_h - self._offset, relwidth=1,
                                height=self._row_h)
        for s in range(pool):
            if s not in shown:
                self._rows[s].place_forget()
                self._slot[s] = None
        first, last = scrollbar_fractions(self._offset, self._count, self._row_h, view_h)
        self._sb.set(first, last)

    def _scroll_to(self, offset: float) -> None:
        """Scroll events (wheel, scrollbar drag) arrive far faster than a screen can show:
        keep the newest position and lay out once per ~12 ms."""
        new = clamp_offset(offset, self._count, self._row_h, self._view_h())
        if new != self._offset:
            self._offset = new
            if self._job is None:
                self._job = self.after(12, self._flush)

    def _flush(self) -> None:
        self._job = None
        self._layout()

    def _yview(self, *args) -> None:
        total = self._count * self._row_h
        if args[0] == "moveto":
            self._scroll_to(float(args[1]) * total)
        elif args[0] == "scroll":
            step = int(args[1]) * (self._view_h() if args[2] == "pages" else _WHEEL_STEP)
            self._scroll_to(self._offset + step)

    def _wheel(self, e: tk.Event) -> None:
        try:
            if not self.winfo_exists() or not self.winfo_ismapped():
                return
            if not str(e.widget).startswith(str(self)):
                return
        except tk.TclError:
            return
        up = getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0
        self._scroll_to(self._offset + (-_WHEEL_STEP if up else _WHEEL_STEP))
