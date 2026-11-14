"""
ui/nav_history.py
-----------------
Back/forward history for the navigation shell. Pure Python (no Tk), so it is
easy to test. Routes are opaque hashable values.
"""

from __future__ import annotations

from typing import Hashable


class NavHistory:
    def __init__(self, limit: int = 100) -> None:
        self._back: list[Hashable] = []
        self._fwd: list[Hashable] = []
        self._limit = limit

    def visit(self, current: Hashable) -> None:
        """Call when leaving `current` for a brand-new page (clears forward)."""
        self._back.append(current)
        del self._back[:-self._limit]
        self._fwd.clear()

    def back(self, current: Hashable) -> Hashable | None:
        """Route to go back to, or None. `current` becomes the forward target."""
        if not self._back:
            return None
        self._fwd.append(current)
        return self._back.pop()

    def forward(self, current: Hashable) -> Hashable | None:
        """Route to go forward to, or None. `current` becomes the back target."""
        if not self._fwd:
            return None
        self._back.append(current)
        return self._fwd.pop()

    @property
    def can_back(self) -> bool:
        return bool(self._back)

    @property
    def can_forward(self) -> bool:
        return bool(self._fwd)
