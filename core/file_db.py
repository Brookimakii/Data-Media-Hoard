"""
core/file_db.py
---------------
SQLite database tracking hoard data (media + fanfiction).

Schema
------
artists_media
    id              INTEGER PRIMARY KEY AUTOINCREMENT
    filename        TEXT NOT NULL
    filepath        TEXT NOT NULL UNIQUE
    artist          TEXT
    site            TEXT
    source_url      TEXT
    file_size       INTEGER   -- bytes
    downloaded_at   TEXT      -- ISO-8601
    upload_status   TEXT DEFAULT 'pending'
                    -- 'pending' | 'uploaded' | 'ignored'

Usage
-----
    from core.file_db import FileDB
    db = FileDB("hoard.db")
    db.register(filename="img.jpg", filepath="/dl/artist/img.jpg",
                artist="artist", site="pixiv", source_url="https://...",
                file_size=102400)
    db.set_status("/dl/artist/img.jpg", "uploaded")
    rows = db.query(artist="artist", status="pending")
"""

from __future__ import annotations

import logging
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

STATUS_PENDING  = "pending"
STATUS_UPLOADED = "uploaded"
STATUS_IGNORED  = "ignored"
ALL_STATUSES    = (STATUS_PENDING, STATUS_UPLOADED, STATUS_IGNORED)


class FileDB:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._con  = sqlite3.connect(str(self._path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._init_schema()
        log.debug("FileDB opened: %s", self._path)

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        self._con.executescript("""
            CREATE TABLE IF NOT EXISTS artists_media (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                filename       TEXT    NOT NULL,
                filepath       TEXT    NOT NULL UNIQUE,
                artist         TEXT,
                site           TEXT,
                source_url     TEXT,
                file_size      INTEGER,
                downloaded_at  TEXT,
                upload_status  TEXT    NOT NULL DEFAULT 'pending',
                tags           TEXT,
                sha256         TEXT,
                phash          TEXT,
                hashed_at      TEXT
            );

            -- Future-proof table for fic records and status tracking
            CREATE TABLE IF NOT EXISTS fanfiction (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                url            TEXT    NOT NULL UNIQUE,
                title          TEXT,
                fandom         TEXT,
                status         TEXT,
                updated_at     TEXT,
                source_file    TEXT,
                created_at     TEXT,
                last_synced_at TEXT,
                enabled        INTEGER,
                is_series      INTEGER,
                word_count     TEXT,
                chapters       TEXT,
                summary        TEXT,
                categories     TEXT,
                tags           TEXT,
                authors        TEXT,
                rating         TEXT,
                fandoms_list   TEXT,
                relationships  TEXT,
                characters     TEXT,
                warnings       TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_am_artist   ON artists_media(artist);
            CREATE INDEX IF NOT EXISTS idx_am_site     ON artists_media(site);
            CREATE INDEX IF NOT EXISTS idx_am_status   ON artists_media(upload_status);
            CREATE INDEX IF NOT EXISTS idx_am_filepath ON artists_media(filepath);
            CREATE INDEX IF NOT EXISTS idx_ff_fandom   ON fanfiction(fandom);
            CREATE INDEX IF NOT EXISTS idx_ff_status   ON fanfiction(status);
        """)

        # Backfill columns for existing artists_media tables created by older versions.
        try:
            cols = {
                row[1]
                for row in self._con.execute("PRAGMA table_info(artists_media)").fetchall()
            }
            add_cols = {
                "tags":      "TEXT",
                "sha256":    "TEXT",
                "phash":     "TEXT",
                "hashed_at": "TEXT",
            }
            for name, typ in add_cols.items():
                if name not in cols:
                    self._con.execute(f"ALTER TABLE artists_media ADD COLUMN {name} {typ}")
        except Exception:
            pass

        # Hash indexes created only after the columns are guaranteed to exist
        # (old DBs need the ALTER TABLE above to run first).
        try:
            self._con.executescript("""
                CREATE INDEX IF NOT EXISTS idx_am_sha256 ON artists_media(sha256);
                CREATE INDEX IF NOT EXISTS idx_am_phash  ON artists_media(phash);
            """)
        except Exception:
            pass

        # Backfill columns for existing fanfiction tables created by older versions.
        try:
            cols = {
                row[1]
                for row in self._con.execute("PRAGMA table_info(fanfiction)").fetchall()
            }
            add_cols = {
                "last_synced_at": "TEXT",
                "enabled": "INTEGER",
                "is_series": "INTEGER",
                "word_count": "TEXT",
                "chapters": "TEXT",
                "summary": "TEXT",
                "categories": "TEXT",
                "tags": "TEXT",
                "authors": "TEXT",
                "rating": "TEXT",
                "fandoms_list": "TEXT",
                "relationships": "TEXT",
                "characters": "TEXT",
                "warnings": "TEXT",
            }
            for name, typ in add_cols.items():
                if name not in cols:
                    self._con.execute(f"ALTER TABLE fanfiction ADD COLUMN {name} {typ}")
        except Exception:
            pass

        # One-time migration from legacy table name "downloads" to "artists_media".
        try:
            row = self._con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='downloads'"
            ).fetchone()
            legacy_exists = bool(row)
            if legacy_exists:
                am_count = self._con.execute("SELECT COUNT(*) FROM artists_media").fetchone()[0]
                if am_count == 0:
                    self._con.execute("""
                        INSERT OR IGNORE INTO artists_media
                            (id, filename, filepath, artist, site, source_url,
                             file_size, downloaded_at, upload_status)
                        SELECT id, filename, filepath, artist, site, source_url,
                               file_size, downloaded_at, upload_status
                        FROM downloads
                    """)
        except Exception:
            pass
        self._con.commit()

    # ── Write ─────────────────────────────────────────────────────────────────

    def register(self, filename: str, filepath: str | Path,
                 artist: str = "", site: str = "",
                 source_url: str = "", file_size: int = 0) -> None:
        """
        Insert or update a downloaded file record.
        If the filepath already exists, updates artist/site/source/size
        but preserves the existing upload_status.
        """
        fp  = str(filepath)
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute("""
            INSERT INTO artists_media
                (filename, filepath, artist, site, source_url, file_size, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filepath) DO UPDATE SET
                artist        = excluded.artist,
                site          = excluded.site,
                source_url    = excluded.source_url,
                file_size     = excluded.file_size,
                downloaded_at = excluded.downloaded_at
        """, (filename, fp, artist, site, source_url, file_size, now))
        self._con.commit()
        log.debug("register: %s (artist=%s site=%s size=%d bytes)",
                  fp, artist or "-", site or "-", file_size)

    def set_status(self, filepath: str | Path, status: str) -> None:
        if status not in ALL_STATUSES:
            raise ValueError(f"Invalid status: {status!r}")
        self._con.execute(
            "UPDATE artists_media SET upload_status = ? WHERE filepath = ?",
            (status, str(filepath)))
        self._con.commit()
        log.debug("set_status: %s -> %s", filepath, status)

    def set_status_by_id(self, row_id: int, status: str) -> None:
        if status not in ALL_STATUSES:
            raise ValueError(f"Invalid status: {status!r}")
        self._con.execute(
            "UPDATE artists_media SET upload_status = ? WHERE id = ?",
            (status, row_id))
        self._con.commit()
        log.debug("set_status_by_id: id=%d -> %s", row_id, status)

    # ── Tags ──────────────────────────────────────────────────────────────────

    def set_tags(self, filepath: str | Path, tags: list[str]) -> None:
        """
        Store this file's tags as a JSON list. Caller is responsible for
        also writing the .txt sidecar if it wants the two kept in sync —
        see utils.media_tags.write_tags() which does both at once.
        """
        fp = str(filepath)
        if not self.get(fp):
            self.register(filename=Path(fp).name, filepath=fp)
        self._con.execute(
            "UPDATE artists_media SET tags = ? WHERE filepath = ?",
            (json.dumps(tags, ensure_ascii=False), fp))
        self._con.commit()
        log.debug("set_tags: %s -> %s", fp, tags)

    def get_tags(self, filepath: str | Path) -> list[str]:
        row = self.get(filepath)
        if not row or not row["tags"]:
            return []
        try:
            return json.loads(row["tags"])
        except (json.JSONDecodeError, TypeError):
            return []

    # ── Hashes (for duplicate detection) ───────────────────────────────────────

    def set_hashes(self, filepath: str | Path,
                   sha256: str = "", phash: str = "") -> None:
        fp = str(filepath)
        if not self.get(fp):
            self.register(filename=Path(fp).name, filepath=fp)
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            "UPDATE artists_media SET sha256 = ?, phash = ?, hashed_at = ? "
            "WHERE filepath = ?",
            (sha256 or None, phash or None, now, fp))
        self._con.commit()
        log.debug("set_hashes: %s sha256=%s phash=%s",
                  fp, (sha256 or "-")[:12], phash or "-")

    def get_hashes(self, filepath: str | Path) -> tuple[str | None, str | None]:
        row = self.get(filepath)
        if not row:
            return None, None
        return row["sha256"], row["phash"]

    def all_hashed(self) -> list[sqlite3.Row]:
        """All rows that have at least a sha256 hash recorded."""
        return self._con.execute(
            "SELECT * FROM artists_media WHERE sha256 IS NOT NULL"
        ).fetchall()

    def find_exact_duplicates(self) -> dict[str, list[sqlite3.Row]]:
        """{sha256: [rows]} for every hash shared by 2+ files."""
        rows = self.all_hashed()
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault(row["sha256"], []).append(row)
        return {h: rs for h, rs in groups.items() if len(rs) > 1}

    def upsert_fanfiction(
        self,
        *,
        url: str,
        title: str = "",
        fandom: str = "",
        status: str = "",
        updated_at: str = "",
        source_file: str = "",
        enabled: bool = True,
        is_series: bool = False,
        word_count: str = "",
        chapters: str = "",
        summary: str = "",
        categories: list | None = None,
        tags: list | None = None,
        authors: list | None = None,
        rating: str = "",
        fandoms_list: list | None = None,
        relationships: list | None = None,
        characters: list | None = None,
        warnings: list | None = None,
    ) -> None:
        """Insert/update one fanfiction record with rich metadata."""
        if not url:
            return
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            """
            INSERT INTO fanfiction
                (url, title, fandom, status, updated_at, source_file,
                 created_at, last_synced_at,
                 enabled, is_series, word_count, chapters,
                 summary, categories, tags, authors, rating,
                 fandoms_list, relationships, characters, warnings)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title         = excluded.title,
                fandom        = excluded.fandom,
                status        = excluded.status,
                updated_at    = excluded.updated_at,
                source_file   = excluded.source_file,
                last_synced_at= excluded.last_synced_at,
                enabled       = excluded.enabled,
                is_series     = excluded.is_series,
                word_count    = excluded.word_count,
                chapters      = excluded.chapters,
                summary       = excluded.summary,
                categories    = excluded.categories,
                tags          = excluded.tags,
                authors       = excluded.authors,
                rating        = excluded.rating,
                fandoms_list  = excluded.fandoms_list,
                relationships = excluded.relationships,
                characters    = excluded.characters,
                warnings      = excluded.warnings
            """,
            (
                url,
                title or "",
                fandom or "",
                status or "",
                updated_at or "",
                source_file or "",
                now,
                now,
                1 if enabled else 0,
                1 if is_series else 0,
                word_count or "",
                chapters or "",
                summary or "",
                json.dumps(categories or [], ensure_ascii=False),
                json.dumps(tags or [], ensure_ascii=False),
                json.dumps(authors or [], ensure_ascii=False),
                rating or "",
                json.dumps(fandoms_list or [], ensure_ascii=False),
                json.dumps(relationships or [], ensure_ascii=False),
                json.dumps(characters or [], ensure_ascii=False),
                json.dumps(warnings or [], ensure_ascii=False),
            ),
        )
        self._con.commit()
        log.debug("upsert_fanfiction: %s (%s) status=%s", title or url, fandom or "-", status or "-")

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, filepath: str | Path) -> sqlite3.Row | None:
        cur = self._con.execute(
            "SELECT * FROM artists_media WHERE filepath = ?", (str(filepath),))
        return cur.fetchone()

    def get_status(self, filepath: str | Path) -> str:
        row = self.get(filepath)
        return row["upload_status"] if row else STATUS_PENDING

    def query(self, artist: str | None = None,
              site: str | None = None,
              status: str | None = None,
              limit: int = 0) -> list[sqlite3.Row]:
        clauses, params = [], []
        if artist:
            clauses.append("artist = ?"); params.append(artist)
        if site:
            clauses.append("site = ?");   params.append(site)
        if status:
            clauses.append("upload_status = ?"); params.append(status)
        sql = "SELECT * FROM artists_media"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY downloaded_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._con.execute(sql, params).fetchall()

    def counts_by_status(self, artist: str | None = None,
                         site: str | None = None) -> dict[str, int]:
        clauses, params = [], []
        if artist:
            clauses.append("artist = ?"); params.append(artist)
        if site:
            clauses.append("site = ?");   params.append(site)
        sql = "SELECT upload_status, COUNT(*) FROM artists_media"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY upload_status"
        rows = self._con.execute(sql, params).fetchall()
        counts = {STATUS_PENDING: 0, STATUS_UPLOADED: 0, STATUS_IGNORED: 0}
        for row in rows:
            counts[row[0]] = row[1]
        return counts

    def total(self, artist: str | None = None,
              site: str | None = None) -> int:
        clauses, params = [], []
        if artist:
            clauses.append("artist = ?"); params.append(artist)
        if site:
            clauses.append("site = ?");   params.append(site)
        sql = "SELECT COUNT(*) FROM artists_media"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self._con.execute(sql, params).fetchone()[0]

    def iter_pending(self, artist: str | None = None) -> Iterator[sqlite3.Row]:
        clauses = ["upload_status = 'pending'"]
        params: list = []
        if artist:
            clauses.append("artist = ?"); params.append(artist)
        sql = ("SELECT * FROM artists_media WHERE "
               + " AND ".join(clauses)
               + " ORDER BY artist, site, filename")
        yield from self._con.execute(sql, params)

    # ── UploadedDNU compatibility ─────────────────────────────────────────────
    # Thin shim so uploader_tab.py can use FileDB as a drop-in replacement.

    def is_uploaded(self, base: Path, file: Path) -> bool:
        return self.get_status(str(file)) == STATUS_UPLOADED

    def is_dnu(self, base: Path, file: Path) -> bool:
        return self.get_status(str(file)) == STATUS_IGNORED

    def is_pending(self, base: Path, file: Path) -> bool:
        return self.get_status(str(file)) == STATUS_PENDING

    def mark_uploaded(self, base: Path, file: Path) -> None:
        # Ensure record exists before setting status
        fp = str(file)
        if not self.get(fp):
            self.register(filename=file.name, filepath=fp)
        self.set_status(fp, STATUS_UPLOADED)

    def mark_dnu(self, base: Path, file: Path) -> None:
        fp = str(file)
        if not self.get(fp):
            self.register(filename=file.name, filepath=fp)
        self.set_status(fp, STATUS_IGNORED)

    def save(self) -> None:
        pass   # SQLite commits immediately — no-op for compatibility

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        log.debug("FileDB closed: %s", self._path)
        self._con.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ── Module-level singleton ────────────────────────────────────────────────────
# Initialised by main.py; accessed everywhere via get_db().

_db: FileDB | None = None


def init_db(path: str | Path) -> FileDB:
    global _db
    _db = FileDB(path)
    return _db


def get_db() -> FileDB | None:
    return _db