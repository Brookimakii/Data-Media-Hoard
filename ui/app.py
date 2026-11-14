"""
ui/app.py
---------
Root window construction and top-level layout.
Creates the root window, applies the ttk theme overrides and mounts the
navigation shell (ui/nav.py). Feature pages are declared in ui/features.py.

Entry point: create_app() → returns the configured tk.Tk root.
Call root.mainloop() in main.py.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ui.theme import (
    ACCENT, BG, BORDER, ENTRY_BG, FG, FG_DIM, FONT_BOLD, PANEL,
)
from ui.nav import Shell
from ui.taskbar import init_taskbar


def _apply_ttk_styles(root: tk.Tk) -> None:
    """Configure all ttk widget styles to match the dark theme."""
    style = ttk.Style(root)
    style.theme_use("default")

    style.configure(
        "TNotebook",
        background=BG, borderwidth=0, tabmargins=[0, 0, 0, 0],
    )
    style.configure(
        "TNotebook.Tab",
        background=BG, foreground=FG_DIM,
        font=FONT_BOLD,
        padding=[20, 10], borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", PANEL), ("active", ENTRY_BG)],
        foreground=[("selected", ACCENT), ("active", FG)],
    )
    style.configure(
        "TScrollbar",
        background=BORDER, troughcolor=PANEL,
        borderwidth=0, arrowsize=12,
    )
    style.configure(
        "Download.Horizontal.TProgressbar",
        troughcolor=ENTRY_BG, background=ACCENT,
        borderwidth=0, thickness=14,
    )


def create_app(root: tk.Tk | None = None, on_progress=None) -> tk.Tk:
    """
    Build and return the configured root window.
    Does not call mainloop() — that is the caller's responsibility.

    root        — existing Tk root to build into (default: create one).
    on_progress — optional callable(text) invoked before each heavy build
                  step, used by the splash screen.
    """
    def _progress(text: str) -> None:
        if on_progress:
            on_progress(text)

    if root is None:
        root = tk.Tk()
    root.title("Booru Manager")
    root.geometry("980x780")
    root.configure(bg=BG)
    root.minsize(720, 560)

    _apply_ttk_styles(root)
    root.after(100, lambda: init_taskbar(root))

    # Existing tabs are intentionally NOT mounted for now — see ui/features.py
    # (set a SubFeature's `builder` to plug one back in).
    _progress("Building navigation…")
    Shell(root)
    _progress("Finishing…")

    # Clicking on any non-interactive surface removes focus from entries/buttons.
    # Skip if the click is inside a Toplevel popup (calendar, dropdown, etc.)
    _FOCUSABLE = (tk.Entry, tk.Text, tk.Listbox, tk.Canvas, ttk.Combobox)
    def _blur_on_click(event: tk.Event) -> None:
        # Walk up the widget hierarchy — if we hit a Toplevel that isn't root, skip
        w = event.widget
        while w is not None:
            if isinstance(w, tk.Toplevel):
                return
            try:
                w = w.master
            except Exception:
                break
        if not isinstance(event.widget, _FOCUSABLE):
            root.focus_set()
    root.bind_all("<Button-1>", _blur_on_click, add="+")

    return root