"""
ui/autocomplete.py
------------------
Autocomplete for a comma separated tag entry (used by the tag form's Implies field).

Type a few letters of the tag under the cursor and a list of matching tags drops
below the entry:
    ↓ / ↑     move in the list          Enter / Tab / click   take the highlighted tag
    Esc       close the list            (anything else just keeps typing)

The text handling (`current_token`, `apply_choice`) is plain functions so it can be
tested without Tk.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

from ui.theme import ACCENT, BORDER, ENTRY_BG, FG, FONT_BTN

_UI = FONT_BTN[0]
_MAX_ROWS = 8
_NAV_KEYS = {"Up", "Down", "Return", "Tab", "Escape", "Shift_L", "Shift_R", "Control_L",
             "Control_R", "Alt_L", "Alt_R", "ISO_Left_Tab"}


def current_token(text: str, cursor: int) -> tuple[int, int, str]:
    """The comma-separated item the cursor is in -> (start, end, typed-so-far).

    `start` skips leading blanks, `end` is the next comma (or the end of the text),
    and the typed part is what sits between `start` and the cursor.
    """
    cursor = max(0, min(cursor, len(text)))
    start = text.rfind(",", 0, cursor) + 1
    while start < len(text) and text[start] in " \t\n" and start < cursor:
        start += 1
    nxt = text.find(",", cursor)
    end = len(text) if nxt == -1 else nxt
    return start, end, text[start:cursor].strip()


def apply_choice(text: str, cursor: int, choice: str) -> tuple[str, int]:
    """Replace the item under the cursor by `choice`. -> (new text, new cursor)."""
    start, end, _typed = current_token(text, cursor)
    head, tail = text[:start], text[end:]
    if tail.strip():                                # a comma follows: keep the rest as is
        new = head + choice + tail
        return new, len(head) + len(choice)
    new = head + choice + ", "                      # last item: get ready for the next one
    return new, len(new)


def other_items(text: str, cursor: int) -> list[str]:
    """Every item of the entry except the one under the cursor."""
    start, end, _typed = current_token(text, cursor)
    rest = text[:start] + "," + text[end:]
    return [p.strip() for p in rest.split(",") if p.strip()]


class Autocomplete:
    """Attach to a tk.Entry. `provider(prefix, exclude)` returns dicts with
    "name", "category" and "via" (synonym that matched, or "")."""

    def __init__(self, entry: tk.Entry, provider: Callable[[str, list[str]], list[dict]],
                 colors: dict[str, str] | None = None,
                 on_change: Callable[[], None] | None = None) -> None:
        self._entry = entry
        self._provider = provider
        self._colors = colors or {}
        self._on_change = on_change
        self._items: list[dict] = []
        self._hide_job: str | None = None
        self._list = tk.Listbox(entry.master, height=1, bg=ENTRY_BG, fg=FG, relief="flat",
                                selectbackground=ACCENT, selectforeground=FG,
                                activestyle="none", exportselection=False,
                                highlightthickness=1, highlightbackground=BORDER,
                                font=(_UI, 10))
        for seq, fn in (("<KeyRelease>", self._on_key), ("<Down>", self._down),
                        ("<Up>", self._up), ("<Return>", self._accept_key),
                        ("<Tab>", self._accept_key), ("<Escape>", self._escape),
                        ("<FocusOut>", self._focus_out), ("<FocusIn>", self._focus_in)):
            entry.bind(seq, fn, add="+")
        self._list.bind("<ButtonRelease-1>", self._click)

    # ── Showing / hiding ──────────────────────────────────────────────────────

    @property
    def visible(self) -> bool:
        return bool(self._items) and self._list.winfo_ismapped()

    def hide(self) -> None:
        self._items = []
        self._list.place_forget()

    def _show(self, items: list[dict]) -> None:
        self._items = items
        if not items:
            self._list.place_forget()
            return
        self._list.delete(0, "end")
        for it in items:
            tail = f"   ← {it['via']}" if it.get("via") else ""
            self._list.insert("end", f"{it['name']}   ·  {it['category']}{tail}")
        for i, it in enumerate(items):
            self._list.itemconfig(i, fg=self._colors.get(it["category"], FG))
        self._list.config(height=min(len(items), _MAX_ROWS))
        self._list.selection_clear(0, "end")
        self._list.selection_set(0)
        self._list.place(in_=self._entry, x=0, y=self._entry.winfo_height(), relwidth=1.0)
        self._list.lift()

    def refresh(self) -> None:
        text = self._entry.get()
        cursor = self._entry.index("insert")
        _s, _e, typed = current_token(text, cursor)
        if not typed:
            self.hide()
            return
        self._show(self._provider(typed, other_items(text, cursor))[:_MAX_ROWS])

    # ── Events ────────────────────────────────────────────────────────────────

    def _on_key(self, e: tk.Event) -> None:
        if e.keysym in _NAV_KEYS:
            return
        self.refresh()

    def _selected(self) -> int:
        sel = self._list.curselection()
        return int(sel[0]) if sel else 0

    def _move(self, step: int) -> str | None:
        if not self.visible:
            return None
        i = (self._selected() + step) % len(self._items)
        self._list.selection_clear(0, "end")
        self._list.selection_set(i)
        self._list.see(i)
        return "break"

    def _down(self, _e: tk.Event) -> str | None:
        return self._move(1)

    def _up(self, _e: tk.Event) -> str | None:
        return self._move(-1)

    def _accept_key(self, _e: tk.Event) -> str | None:
        if not self.visible:
            return None
        self._take(self._selected())
        return "break"

    def _click(self, _e: tk.Event) -> None:
        sel = self._list.curselection()
        if sel:
            self._take(int(sel[0]))

    def _take(self, index: int) -> None:
        if not 0 <= index < len(self._items):
            return
        text, cursor = self._entry.get(), self._entry.index("insert")
        new, pos = apply_choice(text, cursor, self._items[index]["name"])
        self._entry.delete(0, "end")
        self._entry.insert(0, new)
        self._entry.icursor(pos)
        self.hide()
        self._entry.focus_set()
        if self._on_change:
            self._on_change()

    def _escape(self, _e: tk.Event) -> str | None:
        if not self.visible:
            return None                      # let the page's own Escape (cancel) run
        self.hide()
        return "break"

    def _focus_out(self, _e: tk.Event) -> None:
        # a click on the list takes focus away first: wait a moment before closing
        self._hide_job = self._entry.after(180, self.hide)

    def _focus_in(self, _e: tk.Event) -> None:
        if self._hide_job is not None:
            self._entry.after_cancel(self._hide_job)
            self._hide_job = None
