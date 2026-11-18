"""
core/tag_store.py
-----------------
Local-first storage for tags, with undo / redo and an append-only history.
Same rules as core/artist_store.py (soft delete, before/after snapshots, batches
undo as one, nothing is ever erased). No UI, no network.

Tag fields
    name        lowercase, words joined by "_" (e621 style)
    category    general | artist | copyright | character | species | meta | lore
    wiki        free text
    aliases[]   synonyms: other names that mean this tag
    implies[]   tags that are implied by this one (this tag => each of them)

Rules checked on every save
  * the name is unique among live tags and cannot also be a synonym of any tag
  * a synonym cannot be used twice (by this tag or another one)
  * a tag cannot imply itself or one of its own synonyms
  * implications cannot loop (a => b => a)
Implications that point to a tag you do not have yet are allowed.

Sync bookkeeping (used by core/tag_sync.py), same idea as the artists:
    remote_id    id of the tag on the booru server (None = unknown / never pushed)
    pushed_json  the snapshot that is currently on the server
    sync_error   last push error (cleared by any local change or a retry)
A tag's status is derived by comparing the local snapshot with `pushed_json`:
synced | pending | failed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

CATEGORIES = ("general", "artist", "copyright", "character", "species", "meta", "lore")
SORTS = {
    "name": "Name",
    "category": "Type",
    "implies": "Implications",
    "aliases": "Synonyms",
    "updated": "Last edited",
}


class TagError(Exception):
    """User-facing problem (bad input, name already used, ...)."""


# ── Input cleaning ────────────────────────────────────────────────────────────

def norm_tag(text: str) -> str:
    """'Blue  Eyes ' -> 'blue_eyes'."""
    return re.sub(r"[\s_]+", "_", str(text).strip().lower()).strip("_")


def parse_tags(text: str) -> list[str]:
    """'a, b\\nc d' -> ['a', 'b', 'c_d'] (comma / newline separated, deduplicated)."""
    seen: set[str] = set()
    out: list[str] = []
    for part in re.split(r"[,\n]", text or ""):
        t = norm_tag(part)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    name = norm_tag(data.get("name", ""))
    if not name:
        raise TagError("Name is required.")
    category = str(data.get("category", "general")).strip().lower()
    if category not in CATEGORIES:
        raise TagError(f"Unknown type '{category}'.")

    def as_list(v: Any) -> list[str]:
        return parse_tags(v) if isinstance(v, str) else parse_tags(",".join(v or []))

    aliases = [a for a in as_list(data.get("aliases")) if a != name]
    implies = [i for i in as_list(data.get("implies")) if i != name]
    return {"name": name, "category": category, "wiki": str(data.get("wiki", "")).strip(),
            "aliases": aliases, "implies": implies}


FIELDS = ("name", "category", "wiki", "aliases", "implies", "deleted")


def changed_fields(before: dict | None, after: dict | None) -> list[str]:
    if before is None or after is None:
        return []
    return [k for k in FIELDS if before.get(k) != after.get(k)]


def plan(local: dict, pushed: dict | None) -> str:
    """What a push has to do: "noop", "push" (create / update) or "delete"."""
    if local.get("deleted"):
        return "noop" if (pushed is None or pushed.get("deleted")) else "delete"
    if pushed is not None and not pushed.get("deleted") and pushed == local:
        return "noop"
    return "push"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ── Store ─────────────────────────────────────────────────────────────────────

class TagStore:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con
        self._con.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._con.executescript("""
                CREATE TABLE IF NOT EXISTS tags (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT NOT NULL,
                    category   TEXT NOT NULL DEFAULT 'general',
                    wiki       TEXT NOT NULL DEFAULT '',
                    aliases    TEXT NOT NULL DEFAULT '[]',
                    implies    TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_tags_name
                    ON tags(name COLLATE NOCASE) WHERE deleted_at IS NULL;

                CREATE TABLE IF NOT EXISTS tag_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id    TEXT NOT NULL,
                    tag_id      INTEGER NOT NULL,
                    action      TEXT NOT NULL,
                    before_json TEXT,
                    after_json  TEXT,
                    state       TEXT NOT NULL DEFAULT 'applied',
                    at          TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_tag_history_tag ON tag_history(tag_id);
                CREATE INDEX IF NOT EXISTS ix_tag_history_state ON tag_history(state);
            """)
            cols = {r["name"] for r in self._con.execute("PRAGMA table_info(tags)")}
            for name, kind in (("remote_id", "INTEGER"), ("pushed_json", "TEXT"),
                               ("sync_error", "TEXT"), ("sync_at", "TEXT")):
                if name not in cols:
                    self._con.execute(f"ALTER TABLE tags ADD COLUMN {name} {kind}")
            self._con.commit()

    # ── Row <-> snapshot ──────────────────────────────────────────────────────

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
        return {"name": row["name"], "category": row["category"], "wiki": row["wiki"],
                "aliases": json.loads(row["aliases"]), "implies": json.loads(row["implies"]),
                "deleted": row["deleted_at"] is not None}

    @staticmethod
    def _pushed(row: sqlite3.Row) -> dict | None:
        return json.loads(row["pushed_json"]) if row["pushed_json"] else None

    @classmethod
    def _full(cls, row: sqlite3.Row) -> dict[str, Any]:
        """Snapshot + id + sync status."""
        snap = cls._snapshot(row)
        if row["sync_error"]:
            status = "failed"
        else:
            status = "synced" if plan(snap, cls._pushed(row)) == "noop" else "pending"
        snap.update(id=row["id"], remote_id=row["remote_id"], sync_error=row["sync_error"],
                    sync_status=status)
        return snap

    def _row(self, tag_id: int) -> sqlite3.Row:
        row = self._con.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
        if row is None:
            raise TagError(f"Tag #{tag_id} does not exist.")
        return row

    def get(self, tag_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._row(tag_id)
            snap = self._full(row)
            snap.update(created_at=row["created_at"], updated_at=row["updated_at"])
            return snap

    def find(self, name: str) -> dict[str, Any] | None:
        """Live tag called `name` or having it as a synonym (exact match)."""
        name = norm_tag(name)
        if not name:
            return None
        with self._lock:
            r = self._con.execute("SELECT * FROM tags WHERE deleted_at IS NULL "
                                  "AND name = ? COLLATE NOCASE", (name,)).fetchone()
            if r is None:
                r = self._con.execute(
                    "SELECT * FROM tags WHERE deleted_at IS NULL AND aliases LIKE ?",
                    (f'%"{name}"%',)).fetchone()
            return None if r is None else self._full(r)

    # ── Queries ───────────────────────────────────────────────────────────────

    @staticmethod
    def _where(query: str, category: str) -> tuple[str, list[Any]]:
        sql, args = "deleted_at IS NULL", []
        if category and category != "all":
            sql += " AND category = ?"
            args.append(category)
        query = (query or "").strip()
        if query:
            like = _like(norm_tag(query) or query)
            sql += (" AND (name LIKE ? ESCAPE '\\' OR aliases LIKE ? ESCAPE '\\'"
                    " OR implies LIKE ? ESCAPE '\\')")
            args += [like] * 3
        return sql, args

    @staticmethod
    def _order(sort: str, descending: bool) -> str:
        cat = "CASE category " + " ".join(
            f"WHEN '{c}' THEN {i}" for i, c in enumerate(CATEGORIES)) + " END"
        d = "DESC" if descending else "ASC"
        by_name = "name COLLATE NOCASE ASC"
        return {
            "name": f"name COLLATE NOCASE {d}",
            "category": f"{cat} {d}, {by_name}",
            "implies": f"json_array_length(implies) {d}, {by_name}",
            "aliases": f"json_array_length(aliases) {d}, {by_name}",
            "updated": f"updated_at {d}, {by_name}",
        }.get(sort, by_name)

    def search(self, query: str = "", category: str = "all", sort: str = "name",
               descending: bool = False, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        where, args = self._where(query, category)
        with self._lock:
            rows = self._con.execute(
                f"SELECT * FROM tags WHERE {where} ORDER BY {self._order(sort, descending)} "
                "LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            return [self._full(r) for r in rows]

    def count(self, query: str = "", category: str = "all") -> int:
        where, args = self._where(query, category)
        with self._lock:
            return self._con.execute(f"SELECT COUNT(*) FROM tags WHERE {where}",
                                     args).fetchone()[0]

    def category_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._con.execute("SELECT category, COUNT(*) n FROM tags "
                                     "WHERE deleted_at IS NULL GROUP BY category").fetchall()
        return {r["category"]: r["n"] for r in rows}

    def rank(self, tag_id: int, query: str = "", category: str = "all", sort: str = "name",
             descending: bool = False) -> int:
        """0-based position of a tag in the `search()` order."""
        where, args = self._where(query, category)
        with self._lock:
            ids = [r[0] for r in self._con.execute(
                f"SELECT id FROM tags WHERE {where} ORDER BY {self._order(sort, descending)}",
                args)]
        return ids.index(tag_id) if tag_id in ids else 0

    def implied_by(self, name: str) -> list[str]:
        """Names of live tags that imply `name` (directly)."""
        name = norm_tag(name)
        with self._lock:
            rows = self._con.execute(
                "SELECT name, implies FROM tags WHERE deleted_at IS NULL AND implies LIKE ?",
                (f'%"{name}"%',)).fetchall()
        return sorted(r["name"] for r in rows if name in json.loads(r["implies"]))

    def implication_closure(self, name: str) -> list[str]:
        """Everything `name` implies, directly or through other tags (excluding itself)."""
        graph = self._graph()
        seen: list[str] = []
        todo = list(graph.get(name, []))
        while todo:
            t = todo.pop(0)
            if t not in seen and t != name:
                seen.append(t)
                todo.extend(graph.get(t, []))
        return seen

    # ── Validation helpers ────────────────────────────────────────────────────

    def _graph(self, exclude_id: int | None = None) -> dict[str, list[str]]:
        """name -> implied names (synonyms resolved to the real tag name)."""
        rows = self._con.execute(
            "SELECT id, name, aliases, implies FROM tags WHERE deleted_at IS NULL").fetchall()
        alias_to_name = {}
        for r in rows:
            for a in json.loads(r["aliases"]):
                alias_to_name[a] = r["name"]
        graph: dict[str, list[str]] = {}
        for r in rows:
            if r["id"] == exclude_id:
                continue
            graph[r["name"]] = [alias_to_name.get(i, i) for i in json.loads(r["implies"])]
        return graph

    def _check(self, snap: dict[str, Any], tag_id: int | None) -> None:
        """Raise TagError when `snap` would break a rule."""
        name = snap["name"]
        rows = self._con.execute(
            "SELECT id, name, aliases FROM tags WHERE deleted_at IS NULL").fetchall()
        taken_names = {r["name"]: r["id"] for r in rows if r["id"] != tag_id}
        taken_alias = {}
        for r in rows:
            if r["id"] != tag_id:
                for a in json.loads(r["aliases"]):
                    taken_alias[a] = r["name"]
        if name in taken_alias:
            raise TagError(f"'{name}' is already a synonym of '{taken_alias[name]}'.")
        for a in snap["aliases"]:
            if a in taken_names:
                raise TagError(f"Synonym '{a}' is already a tag of its own.")
            if a in taken_alias:
                raise TagError(f"Synonym '{a}' already belongs to '{taken_alias[a]}'.")
        own = {name, *snap["aliases"]}
        for i in snap["implies"]:
            if i in own:
                raise TagError(f"A tag cannot imply itself ('{i}').")
        # loop check: can any implied tag lead back to this one?
        graph = self._graph(exclude_id=tag_id)
        alias_to_name = {a: n for n, al in
                         ((r["name"], json.loads(r["aliases"])) for r in rows
                          if r["id"] != tag_id) for a in al}
        todo = [alias_to_name.get(i, i) for i in snap["implies"]]
        seen: set[str] = set()
        while todo:
            t = todo.pop()
            if t == name:
                raise TagError("These implications would form a loop.")
            if t not in seen:
                seen.add(t)
                todo.extend(graph.get(t, []))

    def name_exists(self, name: str) -> bool:
        return self.find(name) is not None

    # ── Low-level writes (no history) ─────────────────────────────────────────

    def _write_fields(self, tag_id: int, snap: dict, now: str) -> None:
        self._con.execute(
            "UPDATE tags SET name=?, category=?, wiki=?, aliases=?, implies=?, updated_at=? "
            "WHERE id=?",
            (snap["name"], snap["category"], snap["wiki"],
             json.dumps(snap["aliases"], ensure_ascii=False),
             json.dumps(snap["implies"], ensure_ascii=False), now, tag_id))

    def _apply(self, tag_id: int, snap: dict | None) -> None:
        now = _now()
        self._con.execute("UPDATE tags SET sync_error = NULL WHERE id = ?", (tag_id,))
        try:
            if snap is None or snap.get("deleted"):
                self._con.execute("UPDATE tags SET deleted_at = COALESCE(deleted_at, ?), "
                                  "updated_at = ? WHERE id = ?", (now, now, tag_id))
                if snap is not None:
                    self._write_fields(tag_id, snap, now)
            else:
                self._write_fields(tag_id, snap, now)
                self._con.execute("UPDATE tags SET deleted_at = NULL WHERE id = ?", (tag_id,))
        except sqlite3.IntegrityError as e:
            raise TagError(f"Another tag already uses the name "
                           f"'{(snap or {}).get('name', '?')}'.") from e

    def _record(self, batch: str, tag_id: int, action: str,
                before: dict | None, after: dict | None) -> None:
        self._con.execute(
            "INSERT INTO tag_history (batch_id, tag_id, action, before_json, after_json, at) "
            "VALUES (?,?,?,?,?,?)",
            (batch, tag_id, action,
             None if before is None else json.dumps(before, ensure_ascii=False),
             None if after is None else json.dumps(after, ensure_ascii=False), _now()))

    def _discard_redo(self) -> None:
        self._con.execute("UPDATE tag_history SET state='discarded' WHERE state='undone'")

    # ── Public edits ──────────────────────────────────────────────────────────

    def _insert(self, data: dict[str, Any], batch: str, action: str) -> int:
        snap = {**normalize(data), "deleted": False}
        self._check(snap, None)
        now = _now()
        try:
            cur = self._con.execute(
                "INSERT INTO tags (name, category, wiki, aliases, implies, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?)",
                (snap["name"], snap["category"], snap["wiki"],
                 json.dumps(snap["aliases"], ensure_ascii=False),
                 json.dumps(snap["implies"], ensure_ascii=False), now, now))
        except sqlite3.IntegrityError as e:
            raise TagError(f"A tag named '{snap['name']}' already exists.") from e
        self._record(batch, cur.lastrowid, action, None, snap)
        return cur.lastrowid

    def create(self, data: dict[str, Any]) -> int:
        with self._lock, self._con:
            self._discard_redo()
            return self._insert(data, uuid.uuid4().hex, "create")

    def edit(self, tag_id: int, data: dict[str, Any]) -> bool:
        """Returns False (records nothing) when nothing changed."""
        with self._lock, self._con:
            row = self._row(tag_id)
            if row["deleted_at"] is not None:
                raise TagError("This tag is deleted. Restore it first.")
            before = self._snapshot(row)
            after = {**normalize(data), "deleted": False}
            if before == after:
                return False
            self._check(after, tag_id)
            self._discard_redo()
            self._apply(tag_id, after)
            self._record(uuid.uuid4().hex, tag_id, "edit", before, after)
            return True

    def delete(self, tag_id: int) -> None:
        with self._lock, self._con:
            row = self._row(tag_id)
            if row["deleted_at"] is not None:
                return
            before = self._snapshot(row)
            after = {**before, "deleted": True}
            self._discard_redo()
            self._apply(tag_id, after)
            self._record(uuid.uuid4().hex, tag_id, "delete", before, after)

    def import_many(self, items: list[dict[str, Any]]) -> tuple[int, int]:
        """Create every valid item as one undoable batch. Returns (added, skipped)."""
        batch = uuid.uuid4().hex
        added = skipped = 0
        with self._lock, self._con:
            self._discard_redo()
            for item in items:
                try:
                    self._con.execute("SAVEPOINT item")
                    self._insert(item, batch, "import")
                    self._con.execute("RELEASE item")
                    added += 1
                except TagError:
                    self._con.execute("ROLLBACK TO item")
                    self._con.execute("RELEASE item")
                    skipped += 1
        return added, skipped

    def import_remote(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Merge tags fetched from the server, as one undoable batch.

        Each item: name, category, wiki, aliases, implies, remote_id.
          * unknown name              -> added
          * known, nothing unpushed   -> updated to the server's version
          * known, local edits waiting -> kept as they are (they will be pushed)
        Returns {"added", "updated", "same", "kept", "skipped", "errors"}.
        """
        batch = uuid.uuid4().hex
        out: dict[str, Any] = {"added": 0, "updated": 0, "same": 0, "kept": 0,
                               "skipped": 0, "errors": []}
        with self._lock, self._con:
            self._discard_redo()
            for item in items:
                try:
                    self._con.execute("SAVEPOINT remote_item")
                    kind = self._merge_remote(item, batch)
                    self._con.execute("RELEASE remote_item")
                    out[kind] += 1
                except TagError as e:
                    self._con.execute("ROLLBACK TO remote_item")
                    self._con.execute("RELEASE remote_item")
                    out["skipped"] += 1
                    out["errors"].append(f"{item.get('name', '?')}: {e}")
        return out

    def _merge_remote(self, item: dict[str, Any], batch: str) -> str:
        snap = {**normalize(item), "deleted": False}
        rid = item.get("remote_id")
        row = self._con.execute(
            "SELECT * FROM tags WHERE deleted_at IS NULL AND name = ? COLLATE NOCASE",
            (snap["name"],)).fetchone()
        if row is None:
            tag_id = self._insert(snap, batch, "fetch")
            self._con.execute("UPDATE tags SET remote_id=?, pushed_json=?, sync_at=? WHERE id=?",
                              (rid, json.dumps(snap, ensure_ascii=False), _now(), tag_id))
            return "added"
        full = self._full(row)
        before = self._snapshot(row)
        if full["sync_status"] != "synced":
            return "kept"
        if before == snap:
            self._con.execute("UPDATE tags SET remote_id=?, pushed_json=?, sync_at=? WHERE id=?",
                              (rid, json.dumps(snap, ensure_ascii=False), _now(), row["id"]))
            return "same"
        self._check(snap, row["id"])
        self._apply(row["id"], snap)
        self._record(batch, row["id"], "fetch", before, snap)
        self._con.execute("UPDATE tags SET remote_id=?, pushed_json=?, sync_at=? WHERE id=?",
                          (rid, json.dumps(snap, ensure_ascii=False), _now(), row["id"]))
        return "updated"

    # ── Autocomplete ──────────────────────────────────────────────────────────

    def suggest(self, prefix: str, limit: int = 8,
                exclude: tuple[str, ...] | set[str] = ()) -> list[dict[str, str]]:
        """Tags to offer while typing: names starting with `prefix` first, then names
        containing it, then tags that have it as a synonym ("via" says which)."""
        p = norm_tag(prefix)
        if not p:
            return []
        skip = {norm_tag(x) for x in exclude}
        found: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(rows, via_alias: bool) -> None:
            for r in rows:
                if len(found) >= limit:
                    return
                if r["name"] in seen or r["name"] in skip:
                    continue
                seen.add(r["name"])
                via = ""
                if via_alias:
                    via = next((a for a in json.loads(r["aliases"]) if p in a), "")
                found.append({"name": r["name"], "category": r["category"], "via": via})

        with self._lock:
            q = "SELECT name, category, aliases FROM tags WHERE deleted_at IS NULL AND "
            add(self._con.execute(q + "name LIKE ? ESCAPE '\\' ORDER BY name LIMIT 40",
                                  (_like(p)[1:],)), False)
            add(self._con.execute(q + "name LIKE ? ESCAPE '\\' ORDER BY name LIMIT 40",
                                  (_like(p),)), False)
            add(self._con.execute(q + "aliases LIKE ? ESCAPE '\\' ORDER BY name LIMIT 40",
                                  (_like(p),)), True)
        return found

    # ── Sync bookkeeping ──────────────────────────────────────────────────────

    def _status_rows(self):
        return self._con.execute("SELECT * FROM tags").fetchall()

    def pending_ids(self) -> list[int]:
        with self._lock:
            return [r["id"] for r in self._status_rows()
                    if not r["sync_error"]
                    and plan(self._snapshot(r), self._pushed(r)) != "noop"]

    def failed_ids(self) -> list[int]:
        with self._lock:
            return [r["id"] for r in self._status_rows() if r["sync_error"]]

    def sync_summary(self) -> dict[str, int]:
        out = {"synced": 0, "pending": 0, "failed": 0}
        with self._lock:
            for r in self._status_rows():
                snap = self._snapshot(r)
                if snap["deleted"] and r["pushed_json"] is None:
                    continue
                if r["sync_error"]:
                    out["failed"] += 1
                elif plan(snap, self._pushed(r)) == "noop":
                    out["synced"] += 1
                else:
                    out["pending"] += 1
        return out

    def sync_view(self, tag_id: int) -> tuple[dict, int | None, dict | None]:
        """(local snapshot, remote id, snapshot last pushed) for one tag."""
        with self._lock:
            row = self._row(tag_id)
            return self._snapshot(row), row["remote_id"], self._pushed(row)

    def mark_pushed(self, tag_id: int, remote_id: int | None, pushed: dict) -> bool:
        """Record a successful push. Returns True when the tag changed meanwhile."""
        with self._lock, self._con:
            self._con.execute(
                "UPDATE tags SET remote_id=?, pushed_json=?, sync_error=NULL, sync_at=? "
                "WHERE id=?", (remote_id, json.dumps(pushed, ensure_ascii=False), _now(), tag_id))
            row = self._row(tag_id)
            return plan(self._snapshot(row), self._pushed(row)) != "noop"

    def mark_failed(self, tag_id: int, message: str) -> None:
        with self._lock, self._con:
            self._con.execute("UPDATE tags SET sync_error=? WHERE id=?", (message, tag_id))

    def clear_error(self, tag_id: int) -> None:
        with self._lock, self._con:
            self._con.execute("UPDATE tags SET sync_error=NULL WHERE id=?", (tag_id,))

    # ── History ───────────────────────────────────────────────────────────────

    def history(self, tag_id: int, limit: int = 8, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM tag_history WHERE tag_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (tag_id, limit, offset)).fetchall()
        out = []
        for r in rows:
            before = json.loads(r["before_json"]) if r["before_json"] else None
            after = json.loads(r["after_json"]) if r["after_json"] else None
            out.append({"id": r["id"], "action": r["action"], "state": r["state"],
                        "at": r["at"], "before": before, "after": after,
                        "changed": changed_fields(before, after)})
        return out

    def history_count(self, tag_id: int) -> int:
        with self._lock:
            return self._con.execute("SELECT COUNT(*) FROM tag_history WHERE tag_id = ?",
                                     (tag_id,)).fetchone()[0]

    def restore_version(self, history_id: int) -> bool:
        """Bring a tag back to the state right after history entry `history_id` (undoable)."""
        with self._lock, self._con:
            h = self._con.execute("SELECT * FROM tag_history WHERE id = ?",
                                  (history_id,)).fetchone()
            if h is None or not h["after_json"]:
                raise TagError("That version cannot be restored.")
            snap = json.loads(h["after_json"])
            if snap.get("deleted"):
                raise TagError("That version is the deleted state; use Undo instead.")
            tag_id = h["tag_id"]
            before = self._snapshot(self._row(tag_id))
            if before == snap:
                return False
            self._check(snap, tag_id)
            self._discard_redo()
            self._apply(tag_id, snap)
            self._record(uuid.uuid4().hex, tag_id, "restore", before, snap)
            return True

    # ── Undo / redo ───────────────────────────────────────────────────────────

    def _next_batch(self, state: str) -> str | None:
        order = "DESC" if state == "applied" else "ASC"
        row = self._con.execute(
            f"SELECT batch_id FROM tag_history WHERE state = ? ORDER BY id {order} LIMIT 1",
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
        return self._shift("applied", "undone", "before_json", "DESC")

    def redo(self) -> list[int]:
        return self._shift("undone", "applied", "after_json", "ASC")

    def _shift(self, from_state: str, to_state: str, apply_col: str, order: str) -> list[int]:
        with self._lock, self._con:
            batch = self._next_batch(from_state)
            if batch is None:
                return []
            rows = self._con.execute(
                f"SELECT * FROM tag_history WHERE batch_id = ? AND state = ? "
                f"ORDER BY id {order}", (batch, from_state)).fetchall()
            ids: list[int] = []
            for r in rows:
                snap = json.loads(r[apply_col]) if r[apply_col] else None
                self._apply(r["tag_id"], snap)
                self._con.execute("UPDATE tag_history SET state = ? WHERE id = ?",
                                  (to_state, r["id"]))
                ids.append(r["tag_id"])
            return ids

    def undo_label(self) -> str:
        return self._label("applied")

    def redo_label(self) -> str:
        return self._label("undone")

    def _label(self, state: str) -> str:
        with self._lock:
            batch = self._next_batch(state)
            if batch is None:
                return ""
            rows = self._con.execute(
                "SELECT action, after_json, before_json FROM tag_history "
                "WHERE batch_id = ? AND state = ? ORDER BY id", (batch, state)).fetchall()
            if len(rows) > 1:
                return f"{rows[0]['action']} of {len(rows)} tags"
            snap = json.loads(rows[0]["after_json"] or rows[0]["before_json"] or "{}")
            return f"{rows[0]['action']} '{snap.get('name', '?')}'"
