"""
ui/app.py
---------
Root window construction and top-level layout.
Assembles the ttk.Notebook, mounts each tab, applies the ttk theme
overrides, and wires up the global scroll router.

Entry point: create_app() → returns the configured tk.Tk root.
Call root.mainloop() in main.py.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ui.theme import (
    ACCENT, BG, BORDER, ENTRY_BG, FG, FG_DIM,
    FONT_BOLD, FONT_HEAD, FONT_MONO, FONT_SUB, PANEL,
)
from ui.scroll import bind_global_scroll
from ui.downloader_tab import build_downloader
from ui.uploader_tab import build_uploader
from ui.fic_tab import build_fic_tracker
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


def _build_title_bar(root: tk.Tk) -> None:
    """Thin branded header above the notebook."""
    bar = tk.Frame(root, bg=BG, pady=12)
    bar.pack(fill="x", padx=30)

    tk.Label(
        bar, text="✦ BOORU MANAGER",
        bg=BG, fg=ACCENT, font=FONT_HEAD,
    ).pack(side="left")

    tk.Label(
        bar, text="personal image database toolkit",
        bg=BG, fg=FG_DIM, font=FONT_SUB,
    ).pack(side="left", padx=(12, 0))

    tk.Frame(root, bg=BORDER, height=1).pack(fill="x")


def create_app() -> tk.Tk:
    """
    Build and return the configured root window.
    Does not call mainloop() — that is the caller's responsibility.
    """
    root = tk.Tk()
    root.title("Booru Manager")
    root.geometry("980x780")
    root.configure(bg=BG)
    root.minsize(720, 560)

    _apply_ttk_styles(root)
    _build_title_bar(root)
    root.after(100, lambda: init_taskbar(root))

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)

    tab_dl  = tk.Frame(nb, bg=PANEL)
    tab_up  = tk.Frame(nb, bg=PANEL)
    tab_fic = tk.Frame(nb, bg=PANEL)
    nb.add(tab_dl,  text="  ↓  Downloader  ")
    nb.add(tab_up,  text="  ↑  Uploader  ")
    nb.add(tab_fic, text="  📖  Fic Tracker  ")

    build_downloader(tab_dl)
    build_uploader(tab_up)
    build_fic_tracker(tab_fic)

    bind_global_scroll(root)

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