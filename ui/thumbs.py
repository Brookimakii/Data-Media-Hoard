"""
ui/thumbs.py
------------
Background thumbnails for the duplicate list.

Previews are made once, during the scan, and stored in hoard.db (core.dupes), so showing
a row is a tiny database read instead of decoding a big image. Only files indexed before
previews existed fall back to decoding the file. One worker thread (newest request first,
so rows you just scrolled to load first); the UI thread polls only while requests are
pending.
"""

from __future__ import annotations

import io
import queue
import sqlite3
import threading
from collections import OrderedDict

from PIL import Image

from core.dupes import DupeIndex

MISSING = object()


class ThumbLoader:
    def __init__(self, box: tuple[int, int] = (72, 56), db_path: str | None = None,
                 capacity: int = 1500) -> None:
        self.box = box
        self._db_path = db_path
        self._cap = capacity
        self._todo: queue.LifoQueue[tuple[str, str]] = queue.LifoQueue()
        self._done: queue.Queue[tuple[str, Image.Image | None]] = queue.Queue()
        self._cache: OrderedDict[str, Image.Image | None] = OrderedDict()
        self._pending: set[str] = set()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return bool(self._pending)

    def get(self, key: str, path: str):
        """The PIL thumbnail, None if unreadable, or MISSING if not loaded yet (queued)."""
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        if key not in self._pending:
            self._pending.add(key)
            self._todo.put((key, path))
            self._ensure_thread()
        return MISSING

    def poll(self) -> list[str]:
        """Keys finished since the last call (UI thread)."""
        out = []
        while True:
            try:
                key, img = self._done.get_nowait()
            except queue.Empty:
                return out
            self._pending.discard(key)
            self._cache[key] = img
            while len(self._cache) > self._cap:
                self._cache.popitem(last=False)
            out.append(key)

    def _ensure_thread(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="thumbs", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        conn = None
        try:
            if self._db_path:
                conn = sqlite3.connect(self._db_path)
        except sqlite3.Error:
            conn = None
        while True:
            key, path = self._todo.get()                 # blocks: zero CPU while idle
            self._done.put((key, self._load(conn, key, path)))

    def _load(self, conn: sqlite3.Connection | None, key: str, path: str) -> Image.Image | None:
        try:
            blob = DupeIndex.read_preview(conn, key) if conn is not None else None
            if blob:
                with Image.open(io.BytesIO(blob)) as im:
                    im.thumbnail(self.box)
                    return im.convert("RGB")
            with Image.open(path) as im:                  # no stored preview: decode the file
                if im.format == "JPEG":
                    im.draft("RGB", (self.box[0] * 2, self.box[1] * 2))
                im.thumbnail(self.box)
                return im.convert("RGB")
        except Exception:
            return None
