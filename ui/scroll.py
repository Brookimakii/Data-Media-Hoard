"""
ui/scroll.py
------------
Global mouse-wheel scroll router.

Routes scroll events to whichever registered canvas the mouse pointer
is currently positioned over, determined geometrically on every event.
This is robust against focus changes, clicks on child widgets, and
nested scrollable areas — the correct canvas is always found regardless
of which widget currently holds keyboard focus or was last clicked.

Usage
-----
    from ui.scroll import register_scroll_canvas, bind_global_scroll

    register_scroll_canvas(my_canvas)   # for every scrollable Canvas
    bind_global_scroll(root)            # once, on the root window
"""

import tkinter as tk

_canvases: list[tk.Canvas] = []
_suspended = False


def suspend_global_scroll(suspended: bool = True) -> None:
    """
    Pause (or resume) the global scroll router entirely.

    Call suspend_global_scroll(True) when a modal Toplevel (e.g. the YAML
    editor) opens on top of the main window, and suspend_global_scroll(False)
    when it closes. While suspended, _global_scroll is a no-op, so wheel
    events over the main window can't scroll anything underneath the modal —
    geometric "is the pointer over a registered canvas" checks have no idea
    a dialog is stacked on top, so this is the only reliable way to stop it.
    """
    global _suspended
    _suspended = suspended


def register_scroll_canvas(canvas: tk.Canvas) -> None:
    """Register a canvas as a scroll target."""
    if canvas not in _canvases:
        _canvases.append(canvas)
    _canvases[:] = [c for c in _canvases if c.winfo_exists()]


def set_scroll_enabled(canvas: tk.Canvas, enabled: bool) -> None:
    """
    Enable or disable scroll routing for a specific canvas.
    Use this to suppress scrolling when the content fits without scrolling.
    """
    if enabled:
        if canvas not in _canvases:
            _canvases.append(canvas)
    else:
        if canvas in _canvases:
            _canvases.remove(canvas)


def _canvas_under_pointer(event: tk.Event) -> tk.Canvas | None:
    """
    Return the topmost registered canvas whose screen rect contains the
    pointer, or None. Checks in reverse registration order so inner
    (later-registered) canvases take priority over outer ones.
    """
    # Pointer in screen coordinates
    px = event.x_root
    py = event.y_root

    for canvas in reversed(_canvases):
        if not canvas.winfo_exists() or not canvas.winfo_ismapped():
            continue
        cx = canvas.winfo_rootx()
        cy = canvas.winfo_rooty()
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        if cx <= px < cx + cw and cy <= py < cy + ch:
            return canvas
    return None


def _global_scroll(event: tk.Event) -> None:
    if _suspended:
        return
    # Let Text widgets handle their own scrolling
    if isinstance(event.widget, tk.Text):
        return
    canvas = _canvas_under_pointer(event)
    if canvas is None:
        return
    if event.num == 4:
        canvas.yview_scroll(-1, "units")
    elif event.num == 5:
        canvas.yview_scroll(1, "units")
    else:
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


def bind_global_scroll(root: tk.Tk) -> None:
    """
    Attach the global scroll handler to the root window.
    Call this once after all tabs are built.
    """
    root.bind_all("<MouseWheel>", _global_scroll)
    root.bind_all("<Button-4>",   _global_scroll)
    root.bind_all("<Button-5>",   _global_scroll)