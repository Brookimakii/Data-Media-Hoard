"""
core/artist_store.py
--------------------
Local-first storage for artist entries, with full undo / redo and an
append-only history. No UI, no network: the Artists page and the (later)
server sync both sit on top of this.

Artist fields (all kept, matching an e621ng artist entry):
    name, other_names[], group_name, urls[], notes, is_active

Rules
  * Deleting is soft: the row gets `deleted_at` and can always be restored.
  * Every change is written to `artist_history` as a before/after snapshot.
  * History is never erased. Entry states:
        applied   — in effect
        undone    — reverted by Undo, can be redone
        discarded — was redoable, then a new action replaced the redo branch
    Older versions stay reachable through restore_version().
  * Changes made together (e.g. an import) share a batch_id and undo as one.
  * Sync bookkeeping (used by core/artist_sync.py):
        remote_id   — id of the artist on the booru server (None = never pushed)
        pushed_json — the snapshot that is currently on the server
        sync_error  — last push error (cleared by any local change or a retry)
    An artist's status is derived by comparing the local snapshot with
    `pushed_json`:  synced | pending | failed.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class ArtistError(Exception):
    """User-facing problem (bad input, name already used, ...)."""


# ── Input cleaning ─────────────────────────────────────────────────────────────

def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = it.casefold()
        if it and key not in seen:
            seen.add(key)
            out.append(it)
    return out


def parse_names(text: str) -> list[str]:
    """'a, b\\nc' -> ['a', 'b', 'c'] (comma or newline separated, deduplicated)."""
    return _dedupe([" ".join(p.split()) for p in re.split(r"[,\n]", text or "")])


def parse_urls(text: str) -> list[str]:
    """One URL per line / whitespace separated, deduplicated."""
    return _dedupe((text or "").split())


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Canonical snapshot (without the `deleted` flag) from raw form values."""
    name = " ".join(str(data.get("name", "")).split())
    if not name:
        raise ArtistError("Name is required.")

    def as_list(v: Any, parser) -> list[str]:
        return parser(v) if isinstance(v, str) else _dedupe(
            [str(x).strip() for x in (v or [])])

    return {
        "name": name,
        "other_names": as_list(data.get("other_names"), parse_names),
        "group_name": " ".join(str(data.get("group_name", "")).split()),
        "urls": as_list(data.get("urls"), parse_urls),
        "notes": str(data.get("notes", "")).strip(),
        "is_active": bool(data.get("is_active", True)),
    }


def changed_fields(before: dict | None, after: dict | None) -> list[str]:
    """Names of the fields that differ between two snapshots."""
    if before is None or after is None:
        return []
    keys = ("name", "other_names", "group_name", "urls", "notes", "is_active", "deleted")
    return [k for k in keys if before.get(k) != after.get(k)]


def plan(local: dict, remote_id: int | None, pushed: dict | None) -> str:
    """What has to be done on the server so it matches `local`.

    Returns "noop", "create", "update", "delete" or "restore".
    ("restore" = the server copy was deleted but the local one is back, e.g. after Undo.)
    """
    deleted = bool(local.get("deleted"))
    if remote_id is None:
        return "noop" if deleted else "create"
    if deleted:
        return "noop" if (pushed and pushed.get("deleted")) else "delete"
    if pushed is None or pushed.get("deleted"):
        return "restore"
    return "noop" if pushed == local else "update"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ── Store ──────────────────────────────────────────────────────────────────────

class ArtistStore:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con
        self._con.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._con.executescript("""
                CREATE TABLE IF NOT EXISTS artists (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT    NOT NULL,
                    other_names TEXT    NOT NULL DEFAULT '[]',
                    group_name  TEXT    NOT NULL DEFAULT '',
                    urls        TEXT    NOT NULL DEFAULT '[]',
                    notes       TEXT    NOT NULL DEFAULT '',
                    is_active   INTEGER NOT NULL DEFAULT 1,
                    remote_id   INTEGER,
                    pushed_json TEXT,
                    created_at  TEXT    NOT NULL,
                    updated_at  TEXT    NOT NULL,
                    deleted_at  TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_artists_name
                    ON artists(name COLLATE NOCASE) WHERE deleted_at IS NULL;

                CREATE TABLE IF NOT EXISTS artist_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id    TEXT    NOT NULL,
                    artist_id   INTEGER NOT NULL,
                    action      TEXT    NOT NULL,
                    before_json TEXT,
                    after_json  TEXT,
                    state       TEXT    NOT NULL DEFAULT 'applied',
                    at          TEXT    NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_artist_history_artist
                    ON artist_history(artist_id);
                CREATE INDEX IF NOT EXISTS ix_artist_history_state
                    ON artist_history(state);
            """)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first release of the table."""
        cols = {r["name"] for r in self._con.execute("PRAGMA table_info(artists)")}
        for name in ("sync_error", "sync_at"):
            if name not in cols:
                self._con.execute(f"ALTER TABLE artists ADD COLUMN {name} TEXT")
        self._con.commit()

    # ── Row <-> snapshot ──────────────────────────────────────────────────────

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "name": row["name"],
            "other_names": json.loads(row["other_names"]),
            "group_name": row["group_name"],
            "urls": json.loads(row["urls"]),
            "notes": row["notes"],
            "is_active": bool(row["is_active"]),
            "deleted": row["deleted_at"] is not None,
        }

    def _row(self, artist_id: int) -> sqlite3.Row:
        row = self._con.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()
        if row is None:
            raise ArtistError(f"Artist #{artist_id} does not exist.")
        return row

    def get(self, artist_id: int) -> dict[str, Any]:
        """Snapshot plus id, timestamps and sync status."""
        with self._lock:
            row = self._row(artist_id)
            snap = self._snapshot(row)
            snap.update(
                id=row["id"], created_at=row["created_at"], updated_at=row["updated_at"],
                remote_id=row["remote_id"], sync_error=row["sync_error"],
                sync_status=self._sync_status(row, snap))
            return snap

    @staticmethod
    def _pushed(row: sqlite3.Row) -> dict | None:
        return json.loads(row["pushed_json"]) if row["pushed_json"] else None

    @classmethod
    def _sync_status(cls, row: sqlite3.Row, snap: dict) -> str:
        if row["sync_error"]:
            return "failed"
        action = plan(snap, row["remote_id"], cls._pushed(row))
        return "synced" if action == "noop" else "pending"

    # ── Queries ───────────────────────────────────────────────────────────────

    @staticmethod
    def _where(query: str) -> tuple[str, list[Any]]:
        sql, args = "deleted_at IS NULL", []
        query = (query or "").strip()
        if query:
            like = _like(query)
            sql += (" AND (name LIKE ? ESCAPE '\\' OR other_names LIKE ? ESCAPE '\\'"
                    " OR group_name LIKE ? ESCAPE '\\' OR urls LIKE ? ESCAPE '\\')")
            args += [like] * 4
        return sql, args

    def search(self, query: str = "", limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        where, args = self._where(query)
        with self._lock:
            rows = self._con.execute(
                f"SELECT * FROM artists WHERE {where} "
                "ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
                (*args, limit, offset)).fetchall()
            out = []
            for r in rows:
                s = self._snapshot(r)
                s.update(id=r["id"], sync_error=r["sync_error"],
                         sync_status=self._sync_status(r, s))
                out.append(s)
            return out

    def count(self, query: str = "") -> int:
        where, args = self._where(query)
        with self._lock:
            return self._con.execute(
                f"SELECT COUNT(*) FROM artists WHERE {where}", args).fetchone()[0]

    def rank(self, artist_id: int, query: str = "") -> int:
        """0-based position of an artist in the sorted `search()` order."""
        where, args = self._where(query)
        with self._lock:
            name = self._row(artist_id)["name"]
            return self._con.execute(
                f"SELECT COUNT(*) FROM artists WHERE {where} "
                "AND name COLLATE NOCASE < ? COLLATE NOCASE", (*args, name)).fetchone()[0]

    def name_exists(self, name: str) -> bool:
        with self._lock:
            return self._con.execute(
                "SELECT 1 FROM artists WHERE deleted_at IS NULL "
                "AND name = ? COLLATE NOCASE", (name,)).fetchone() is not None

    # ── Low-level writes (no history) ─────────────────────────────────────────

    def _apply(self, artist_id: int, snap: dict | None) -> None:
        """Make the row equal to `snap`. None means 'does not exist' (tombstone)."""
        now = _now()
        self._con.execute("UPDATE artists SET sync_error = NULL WHERE id = ?", (artist_id,))
        try:
            if snap is None or snap.get("deleted"):
                self._con.execute(
                    "UPDATE artists SET deleted_at = COALESCE(deleted_at, ?), updated_at = ? "
                    "WHERE id = ?", (now, now, artist_id))
                if snap is not None:      # keep the field values of the tombstone too
                    self._write_fields(artist_id, snap, now)
            else:
                self._write_fields(artist_id, snap, now)
                self._con.execute("UPDATE artists SET deleted_at = NULL WHERE id = ?",
                                  (artist_id,))
        except sqlite3.IntegrityError as e:
            raise ArtistError(
                f"Another artist already uses the name '{(snap or {}).get('name', '?')}'.") from e

    def _write_fields(self, artist_id: int, snap: dict, now: str) -> None:
        self._con.execute(
            "UPDATE artists SET name=?, other_names=?, group_name=?, urls=?, notes=?, "
            "is_active=?, updated_at=? WHERE id=?",
            (snap["name"], json.dumps(snap["other_names"], ensure_ascii=False),
             snap["group_name"], json.dumps(snap["urls"], ensure_ascii=False),
             snap["notes"], int(snap["is_active"]), now, artist_id))

    def _record(self, batch: str, artist_id: int, action: str,
                before: dict | None, after: dict | None) -> None:
        self._con.execute(
            "INSERT INTO artist_history (batch_id, artist_id, action, before_json, "
            "after_json, at) VALUES (?,?,?,?,?,?)",
            (batch, artist_id, action,
             None if before is None else json.dumps(before, ensure_ascii=False),
             None if after is None else json.dumps(after, ensure_ascii=False), _now()))

    def _discard_redo(self) -> None:
        self._con.execute("UPDATE artist_history SET state='discarded' WHERE state='undone'")

    # ── Public edits ──────────────────────────────────────────────────────────

    def _insert(self, data: dict[str, Any], batch: str, action: str) -> int:
        """Insert + history row. Caller owns the transaction."""
        snap = {**normalize(data), "deleted": False}
        now = _now()
        try:
            cur = self._con.execute(
                "INSERT INTO artists (name, other_names, group_name, urls, notes, "
                "is_active, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (snap["name"], json.dumps(snap["other_names"], ensure_ascii=False),
                 snap["group_name"], json.dumps(snap["urls"], ensure_ascii=False),
                 snap["notes"], int(snap["is_active"]), now, now))
        except sqlite3.IntegrityError as e:
            raise ArtistError(f"An artist named '{snap['name']}' already exists.") from e
        self._record(batch, cur.lastrowid, action, None, snap)
        return cur.lastrowid

    def create(self, data: dict[str, Any]) -> int:
        with self._lock, self._con:
            self._discard_redo()
            return self._insert(data, uuid.uuid4().hex, "create")

    def edit(self, artist_id: int, data: dict[str, Any]) -> bool:
        """Returns False (and records nothing) when nothing changed."""
        with self._lock, self._con:
            row = self._row(artist_id)
            if row["deleted_at"] is not None:
                raise ArtistError("This artist is deleted. Restore it first.")
            before = self._snapshot(row)
            after = {**normalize(data), "deleted": False}
            if before == after:
                return False
            self._discard_redo()
            self._apply(artist_id, after)
            self._record(uuid.uuid4().hex, artist_id, "edit", before, after)
            return True

    def delete(self, artist_id: int) -> None:
        with self._lock, self._con:
            row = self._row(artist_id)
            if row["deleted_at"] is not None:
                return
            before = self._snapshot(row)
            after = {**before, "deleted": True}
            self._discard_redo()
            self._apply(artist_id, after)
            self._record(uuid.uuid4().hex, artist_id, "delete", before, after)

    def import_many(self, items: list[dict[str, Any]]) -> tuple[int, int]:
        """Create every item whose name is free, as one undoable batch.

        Returns (added, skipped). Items are normalised; invalid ones are skipped.
        """
        batch = uuid.uuid4().hex
        added = skipped = 0
        with self._lock, self._con:
            self._discard_redo()
            for item in items:
                try:
                    norm = normalize(item)
                except ArtistError:
                    skipped += 1
                    continue
                if self.name_exists(norm["name"]):
                    skipped += 1
                    continue
                self._insert(norm, batch, "import")
                added += 1
        return added, skipped

    # ── Sync bookkeeping ──────────────────────────────────────────────────────

    def _status_rows(self):
        for r in self._con.execute("SELECT * FROM artists").fetchall():
            snap = self._snapshot(r)
            yield r, snap, self._sync_status(r, snap)

    def pending_ids(self) -> list[int]:
        """Artists that differ from the server and have no push error."""
        with self._lock:
            return [r["id"] for r, _s, st in self._status_rows() if st == "pending"]

    def failed_ids(self) -> list[int]:
        with self._lock:
            return [r["id"] for r, _s, st in self._status_rows() if st == "failed"]

    def sync_summary(self) -> dict[str, int]:
        out = {"pending": 0, "failed": 0, "synced": 0}
        with self._lock:
            for _r, _s, st in self._status_rows():
                out[st] += 1
        return out

    def sync_view(self, artist_id: int) -> tuple[dict, int | None, dict | None]:
        """(local snapshot, remote_id, snapshot currently on the server)."""
        with self._lock:
            row = self._row(artist_id)
            return self._snapshot(row), row["remote_id"], self._pushed(row)

    def mark_pushed(self, artist_id: int, remote_id: int | None, pushed: dict) -> bool:
        """Record a successful push of snapshot `pushed`.

        Returns True when the artist changed locally in the meantime (still pending).
        """
        with self._lock, self._con:
            self._con.execute(
                "UPDATE artists SET remote_id = ?, pushed_json = ?, sync_error = NULL, "
                "sync_at = ? WHERE id = ?",
                (remote_id, json.dumps(pushed, ensure_ascii=False), _now(), artist_id))
            row = self._row(artist_id)
            return self._sync_status(row, self._snapshot(row)) == "pending"

    def mark_failed(self, artist_id: int, message: str) -> None:
        with self._lock, self._con:
            self._con.execute(
                "UPDATE artists SET sync_error = ?, sync_at = ? WHERE id = ?",
                (message[:500] or "Push failed.", _now(), artist_id))

    def clear_error(self, artist_id: int) -> None:
        with self._lock, self._con:
            self._con.execute("UPDATE artists SET sync_error = NULL WHERE id = ?", (artist_id,))

    # ── History ───────────────────────────────────────────────────────────────

    def history(self, artist_id: int, limit: int = 8, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM artist_history WHERE artist_id = ? "
                "ORDER BY id DESC LIMIT ? OFFSET ?", (artist_id, limit, offset)).fetchall()
        out = []
        for r in rows:
            before = json.loads(r["before_json"]) if r["before_json"] else None
            after = json.loads(r["after_json"]) if r["after_json"] else None
            out.append({"id": r["id"], "action": r["action"], "state": r["state"],
                        "at": r["at"], "before": before, "after": after,
                        "changed": changed_fields(before, after)})
        return out

    def history_count(self, artist_id: int) -> int:
        with self._lock:
            return self._con.execute(
                "SELECT COUNT(*) FROM artist_history WHERE artist_id = ?",
                (artist_id,)).fetchone()[0]

    def restore_version(self, history_id: int) -> bool:
        """Bring an artist back to the state right after history entry `history_id`.

        Recorded as a new edit, so it can itself be undone.
        """
        with self._lock, self._con:
            h = self._con.execute(
                "SELECT * FROM artist_history WHERE id = ?", (history_id,)).fetchone()
            if h is None or not h["after_json"]:
                raise ArtistError("That version cannot be restored.")
            snap = json.loads(h["after_json"])
            if snap.get("deleted"):
                raise ArtistError("That version is the deleted state; use Undo instead.")
            artist_id = h["artist_id"]
            row = self._row(artist_id)
            before = self._snapshot(row)
            if before == snap:
                return False
            self._discard_redo()
            self._apply(artist_id, snap)
            self._record(uuid.uuid4().hex, artist_id, "restore", before, snap)
            return True

    # ── Undo / redo ───────────────────────────────────────────────────────────

    def _next_batch(self, state: str) -> str | None:
        order = "DESC" if state == "applied" else "ASC"
        row = self._con.execute(
            f"SELECT batch_id FROM artist_history WHERE state = ? ORDER BY id {order} LIMIT 1",
            (state,)).fetchone()
        return row["batch_id"] if row else None

    @property
    def can_undo(self) -> bool:
        with self._lock:
            return self._next_batch("applied") is not None

    @property
    def can_redo(self) -> bool:
        with self._lock:
            return self._next_batch("undone") is not None

    def undo(self) -> list[int]:
        """Revert the latest applied batch. Returns the affected artist ids."""
        return self._shift("applied", "undone", "before_json", "DESC")

    def redo(self) -> list[int]:
        """Re-apply the most recently undone batch. Returns the affected artist ids."""
        return self._shift("undone", "applied", "after_json", "ASC")

    def _shift(self, from_state: str, to_state: str, apply_col: str, order: str) -> list[int]:
        with self._lock, self._con:
            batch = self._next_batch(from_state)
            if batch is None:
                return []
            rows = self._con.execute(
                f"SELECT * FROM artist_history WHERE batch_id = ? AND state = ? "
                f"ORDER BY id {order}", (batch, from_state)).fetchall()
            ids: list[int] = []
            for r in rows:
                snap = json.loads(r[apply_col]) if r[apply_col] else None
                self._apply(r["artist_id"], snap)
                self._con.execute("UPDATE artist_history SET state = ? WHERE id = ?",
                                  (to_state, r["id"]))
                ids.append(r["artist_id"])
            return ids

    def undo_label(self) -> str:
        return self._label("applied", "DESC")

    def redo_label(self) -> str:
        return self._label("undone", "ASC")

    def _label(self, state: str, order: str) -> str:
        with self._lock:
            batch = self._next_batch(state)
            if batch is None:
                return ""
            rows = self._con.execute(
                "SELECT action, after_json, before_json FROM artist_history "
                "WHERE batch_id = ? AND state = ? ORDER BY id", (batch, state)).fetchall()
            if len(rows) > 1:
                return f"{rows[0]['action']} of {len(rows)} artists"
            r = rows[0]
            snap = json.loads(r["after_json"] or r["before_json"] or "{}")
            return f"{r['action']} '{snap.get('name', '?')}'"


# ── artists.yaml import ────────────────────────────────────────────────────────

def artists_from_yaml(path: str | Path) -> list[dict[str, Any]]:
    """Read the downloader's artists.yaml into artist-form dicts.

    Every URL found under `media` (any category, string or list) becomes one of
    the artist's URLs. The downloader-only fields (enabled, tags) are ignored.
    """
    from utils.yaml_io import read_yaml

    data = read_yaml(path) or {}
    items: list[dict[str, Any]] = []
    for a in data.get("artists", []) or []:
        if not isinstance(a, dict) or not a.get("name"):
            continue
        urls: list[str] = []
        for category in (a.get("media") or {}).values():
            for value in (category or {}).values():
                urls.extend([value] if isinstance(value, str) else
                            [v for v in (value or []) if isinstance(v, str)])
        items.append({"name": a["name"], "urls": urls,
                      "notes": a.get("notes") or "", "is_active": True})
    return items
