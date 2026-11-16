"""
ui/back_stack.py
----------------
Lets a page use the mouse back button / Alt+Left for its own inner screens first
(e.g. duplicate detail -> list) before the app goes back to the previous page.

    register(handler)   handler() -> True if it handled "back"
    try_back()          called by the navigation shell; True if a handler took it
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

_handlers: list[Callable[[], bool]] = []


def register(handler: Callable[[], bool]) -> None:
    _handlers.append(handler)


def try_back() -> bool:
    for handler in reversed(list(_handlers)):
        try:
            if handler():
                return True
        except tk.TclError:                       # its page was destroyed
            if handler in _handlers:
                _handlers.remove(handler)
    return False
