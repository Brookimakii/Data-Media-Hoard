"""
core/virtual_list.py
--------------------
Arithmetic for a recycling (virtualised) list: only the rows inside the viewport
exist as widgets, they are reused while scrolling. Pure functions, no Tk.
"""

from __future__ import annotations


def max_offset(count: int, row_h: int, view_h: int) -> int:
    return max(0, count * row_h - view_h)


def clamp_offset(offset: float, count: int, row_h: int, view_h: int) -> int:
    return int(min(max(0, offset), max_offset(count, row_h, view_h)))


def visible_rows(offset: int, count: int, row_h: int, view_h: int) -> range:
    """Indexes of the rows that intersect the viewport (empty if there are none)."""
    if count <= 0 or row_h <= 0 or view_h <= 0:
        return range(0)
    first = offset // row_h
    last = min(count - 1, (offset + view_h - 1) // row_h)
    return range(first, last + 1)


def pool_size(view_h: int, row_h: int) -> int:
    """Widgets to keep: enough for a full viewport at any scroll position."""
    return max(1, -(-view_h // row_h) + 1)


def scrollbar_fractions(offset: int, count: int, row_h: int, view_h: int) -> tuple[float, float]:
    total = count * row_h
    if total <= view_h or total <= 0:
        return 0.0, 1.0
    return offset / total, min(1.0, (offset + view_h) / total)


def offset_to_show(index: int, offset: int, row_h: int, view_h: int) -> int:
    """Smallest scroll change that makes row `index` fully visible."""
    top, bottom = index * row_h, (index + 1) * row_h
    if top < offset:
        return top
    if bottom > offset + view_h:
        return bottom - view_h
    return offset
