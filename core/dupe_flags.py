"""
core/dupe_flags.py
------------------
What the user decided during duplicate review, kept in hoard.db:

  dupe_flags    files flagged "do not upload" (+ the reason, e.g. "lower resolution")
  dupe_ignored  pairs the user marked "not duplicates" (never grouped again)

Nothing is ever deleted from disk. The Uploader asks `is_flagged(path)`.
"""

from __future__ import annotations

import sqlite3
import time

from core.dupes import file_key


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


class DupeFlags:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._db = conn
        with self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS dupe_flags (
                    key TEXT PRIMARY KEY, path TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '', flagged_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS dupe_ignored (
                    a TEXT NOT NULL, b TEXT NOT NULL, ignored_at TEXT NOT NULL,
                    PRIMARY KEY (a, b));
                CREATE TABLE IF NOT EXISTS dupe_prefs (key TEXT PRIMARY KEY, value TEXT);
            """)

    # ── do not upload ─────────────────────────────────────────────────────────
    def flag(self, path: str, reason: str = "") -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO dupe_flags VALUES (?,?,?,?)",
                             (file_key(path), str(path), reason, _now()))

    def unflag(self, path: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM dupe_flags WHERE key=?", (file_key(path),))

    def is_flagged(self, path: str) -> bool:
        return self._db.execute("SELECT 1 FROM dupe_flags WHERE key=?",
                                (file_key(path),)).fetchone() is not None

    def reason(self, path: str) -> str:
        r = self._db.execute("SELECT reason FROM dupe_flags WHERE key=?",
                             (file_key(path),)).fetchone()
        return r[0] if r else ""

    def flagged_keys(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT key FROM dupe_flags")}

    # ── not duplicates ────────────────────────────────────────────────────────
    @staticmethod
    def _pair(a: str, b: str) -> tuple[str, str]:
        ka, kb = file_key(a), file_key(b)
        return (ka, kb) if ka <= kb else (kb, ka)

    def ignore_pair(self, a: str, b: str) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO dupe_ignored VALUES (?,?,?)",
                             (*self._pair(a, b), _now()))

    def unignore_pair(self, a: str, b: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM dupe_ignored WHERE a=? AND b=?", self._pair(a, b))

    def ignored_pairs(self) -> set[tuple[str, str]]:
        return {(r[0], r[1]) for r in self._db.execute("SELECT a,b FROM dupe_ignored")}

    # ── small preferences (last scanned folder, similarity) ───────────────────
    def get_pref(self, key: str, default: str = "") -> str:
        r = self._db.execute("SELECT value FROM dupe_prefs WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_pref(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO dupe_prefs VALUES (?,?)", (key, value))
