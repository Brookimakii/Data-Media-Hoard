"""
ui/mouse_nav.py
---------------
Make the mouse thumb buttons (back / forward) and Alt+Left / Alt+Right
navigate the app.

How each platform delivers the thumb buttons:
  * Windows, Tk 8.7+/9:  <Button-4> / <Button-5>
  * Windows, Tk 8.6:     Tk ignores them, but Windows turns them into a
                         WM_APPCOMMAND (browser back/forward) message sent to the
                         top-level window. We subclass that window to catch it.
  * Linux / X11:         <Button-8> / <Button-9>
                         (<Button-4>/<Button-5> are the scroll wheel there, so
                         they are NOT bound on that platform.)

A short debounce stops one physical click from navigating twice if more than
one mechanism reports it.
"""

from __future__ import annotations

import logging
import sys
import time
import tkinter as tk
from typing import Callable

log = logging.getLogger(__name__)

_DEBOUNCE_S = 0.15

# WM_APPCOMMAND values (winuser.h)
_WM_APPCOMMAND = 0x0319
_APPCOMMAND_BROWSER_BACKWARD = 1
_APPCOMMAND_BROWSER_FORWARD = 2


def decode_appcommand(lparam: int) -> str | None:
    """'back' / 'forward' for a WM_APPCOMMAND lParam, else None.

    GET_APPCOMMAND_LPARAM(lParam) == HIWORD(lParam) & ~0xF000
    """
    cmd = ((lparam & 0xFFFFFFFF) >> 16) & 0x0FFF
    if cmd == _APPCOMMAND_BROWSER_BACKWARD:
        return "back"
    if cmd == _APPCOMMAND_BROWSER_FORWARD:
        return "forward"
    return None


def install_mouse_nav(root: tk.Tk, on_back: Callable[[], None],
                      on_forward: Callable[[], None]) -> None:
    last = [0.0]

    def fire(fn: Callable[[], None]) -> str:
        now = time.monotonic()
        if now - last[0] >= _DEBOUNCE_S:
            last[0] = now
            fn()
        return "break"

    back = lambda _e=None: fire(on_back)
    fwd = lambda _e=None: fire(on_forward)

    root.bind_all("<Alt-Left>", back, add="+")
    root.bind_all("<Alt-Right>", fwd, add="+")

    if sys.platform.startswith("win"):
        root.bind_all("<Button-4>", back, add="+")      # Tk 8.7+/9 only
        root.bind_all("<Button-5>", fwd, add="+")
        _hook_appcommand(root, lambda: fire(on_back), lambda: fire(on_forward))
    elif sys.platform != "darwin":
        root.bind_all("<Button-8>", back, add="+")
        root.bind_all("<Button-9>", fwd, add="+")


def _hook_appcommand(root: tk.Tk, back: Callable[[], None],
                     forward: Callable[[], None]) -> None:
    """Windows + Tk 8.6: catch WM_APPCOMMAND on the top-level window.

    Installed on the first <Map> event, because the real top-level window
    handle only exists (and stops changing) once the window is shown.
    """
    installed = [False]

    def install(event: tk.Event) -> None:
        if event.widget is not root or installed[0]:
            return
        installed[0] = True
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
            WNDPROC = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM)

            user32.GetAncestor.restype = wintypes.HWND
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            hwnd = user32.GetAncestor(root.winfo_id(), 2)      # GA_ROOT

            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes = [
                ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM]
            set_long.restype = ctypes.c_void_p
            set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]

            old: list[int | None] = [None]

            def proc(h, msg, wparam, lparam):
                if msg == _WM_APPCOMMAND:
                    action = decode_appcommand(lparam)
                    if action:
                        # Run after the message returns, not inside the window proc
                        root.after(0, back if action == "back" else forward)
                        return 1                                 # handled
                return user32.CallWindowProcW(old[0], h, msg, wparam, lparam)

            new_proc = WNDPROC(proc)
            old[0] = set_long(hwnd, -4, ctypes.cast(new_proc, ctypes.c_void_p))  # GWL_WNDPROC
            # Keep references alive for the life of the app (else: crash on GC)
            root._mouse_nav_keepalive = (new_proc, proc, old)    # type: ignore[attr-defined]
            log.debug("mouse_nav: WM_APPCOMMAND hook installed on hwnd %s", hwnd)
        except Exception:
            log.warning("mouse_nav: could not install thumb-button hook", exc_info=True)

    root.bind("<Map>", install, add="+")
