"""
ui/splash.py
------------
Small borderless loading window shown while the app starts up.

Usage (main.py):
    splash = Splash(root, total_steps=10)
    splash.step("Loading settings…")      # advance bar + change status text
    ...
    splash.close()
"""

from __future__ import annotations

import tkinter as tk

from ui.theme import ACCENT, ACCENT2, BG, BORDER, ENTRY_BG, FG_DIM, FONT_HEAD, FONT_SUB

_W, _H = 440, 190


class Splash:
    def __init__(self, root: tk.Tk, total_steps: int = 10) -> None:
        self._root = root
        self._total = max(1, total_steps)
        self._done = 0

        win = tk.Toplevel(root)
        self._win = win
        win.overrideredirect(True)          # no title bar / borders
        win.configure(bg=ACCENT, padx=1, pady=1)   # 1px accent outline
        win.attributes("-topmost", True)    # always in the foreground

        x = (win.winfo_screenwidth() - _W) // 2
        y = (win.winfo_screenheight() - _H) // 2
        win.geometry(f"{_W}x{_H}+{x}+{y}")

        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True)

        tk.Label(body, text="✦ BOORU MANAGER", bg=BG, fg=ACCENT,
                 font=FONT_HEAD).pack(pady=(34, 2))
        tk.Label(body, text="personal image database toolkit", bg=BG, fg=FG_DIM,
                 font=FONT_SUB).pack()

        self._status = tk.Label(body, text="Starting…", bg=BG, fg=FG_DIM,
                                font=FONT_SUB)
        self._status.pack(pady=(26, 8))

        self._bar_w = _W - 80
        self._bar = tk.Canvas(body, width=self._bar_w, height=6, bg=ENTRY_BG,
                              highlightthickness=1, highlightbackground=BORDER)
        self._bar.pack()
        self._fill = self._bar.create_rectangle(0, 0, 0, 6, fill=ACCENT2, width=0)

        win.lift()
        self._paint()

    def _paint(self) -> None:
        # update() (not just update_idletasks) so the window really appears
        # even though the main thread is about to block on heavy work.
        self._win.update()

    def step(self, text: str = "") -> None:
        """Advance the bar by one step and show `text` as the status line."""
        self._done = min(self._done + 1, self._total)
        if text:
            self._status.config(text=text)
        self._bar.coords(self._fill, 0, 0,
                         self._bar_w * self._done / self._total, 6)
        self._paint()

    def close(self) -> None:
        try:
            self._win.destroy()
        except tk.TclError:
            pass
