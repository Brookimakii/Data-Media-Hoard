"""
core/tagger_data.py
-------------------
Everything the Tagger page needs that is not drawing (no Tk):

    scan_folder(folder)              every image under a folder
    load_tags(db, base, paths)       tags of many images at once (one DB query + one walk)
    parse_query / matches            "dog -cat wol*"  tag search
    Selection                        click / Ctrl+click / Shift+click / select all
    common_tags(lists)               tags of a selection with how many images have each
    aggregate(...)                   AI scores of several images -> suggestions to accept
    ThumbStore                       small previews kept in hoard.db (grid opens instantly)
    AIJobs                           model download + image analysis in background threads
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable

from PIL import Image

from core import ai_tagger
from core.dupes import IMAGE_EXT
from core.file_db import FileDB
from core.tag_store import TagStore, norm_tag
from core.tagging import artist_name

log = logging.getLogger(__name__)

THUMB = 150


# ── Folder and tags ──────────────────────────────────────────────────────────

def scan_folder(folder: str | Path) -> list[Path]:
    """Every image below `folder` in path order. Hidden folders (.tags ...) are skipped."""
    out: list[Path] = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in files:
            if os.path.splitext(f)[1].lower() in IMAGE_EXT:
                out.append(Path(root) / f)
    return sorted(out, key=lambda p: str(p).lower())


def _clean(tags: list[str], artist: str) -> list[str]:
    out: list[str] = []
    for t in tags:
        k = norm_tag(t)
        if k and k != norm_tag(artist) and k not in out:
            out.append(k)
    return out


def load_tags(db: FileDB | None, base: Path, paths: list[Path]) -> dict[Path, list[str]]:
    """{image: tags (without the artist)}. The database answers first; for images it has
    nothing on, the .txt sidecar with the same name is used (found with one walk of `base`)."""
    from_db: dict[str, list[str]] = {}
    if db is not None:
        for row in db.connection.execute(
                "SELECT filepath, tags FROM artists_media WHERE tags IS NOT NULL AND tags != ''"):
            try:
                from_db[row[0]] = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                pass
    sidecars: dict[str, list[Path]] | None = None
    out: dict[Path, list[str]] = {}
    for p in paths:
        artist = artist_name(base, p)
        tags = _clean(from_db.get(str(p), []), artist)
        if not tags:
            if sidecars is None:
                sidecars = {}
                for root, _dirs, files in os.walk(base):
                    for f in files:
                        if f.endswith(".txt"):
                            sidecars.setdefault(f[:-4], []).append(Path(root) / f)
            found = sidecars.get(p.stem, [])
            if found:
                mine = [c for c in found if artist and (base / artist) in c.parents]
                try:
                    text = (mine or found)[0].read_text(encoding="utf-8")
                    tags = _clean(text.replace(",", " ").split(), artist)
                except OSError:
                    tags = []
        out[p] = tags
    return out


# ── Searching ────────────────────────────────────────────────────────────────

def parse_query(text: str) -> tuple[list[str], list[str]]:
    """'dog -cat wol*' -> (['dog', 'wol*'], ['cat'])."""
    inc: list[str] = []
    exc: list[str] = []
    for tok in text.replace(",", " ").split():
        neg = tok.startswith("-") and len(tok) > 1
        t = tok[1:] if neg else tok
        star = t.endswith("*")
        t = norm_tag(t.rstrip("*")) + ("*" if star else "")
        if t and t != "*":
            (exc if neg else inc).append(t)
    return inc, exc


def _has(tags: list[str], want: str) -> bool:
    if want.endswith("*"):
        return any(t.startswith(want[:-1]) for t in tags)
    return want in tags


def matches(tags: list[str], inc: list[str], exc: list[str]) -> bool:
    return all(_has(tags, w) for w in inc) and not any(_has(tags, w) for w in exc)


# ── Selection ────────────────────────────────────────────────────────────────

class Selection:
    """Which grid items are selected (by index), with the usual mouse rules."""

    def __init__(self) -> None:
        self.items: set[int] = set()
        self.anchor: int | None = None

    def click(self, i: int, ctrl: bool = False, shift: bool = False) -> set[int]:
        """Apply a click on item i. Returns the indexes whose look changed."""
        before = set(self.items)
        if shift and self.anchor is not None:
            lo, hi = sorted((self.anchor, i))
            rng = set(range(lo, hi + 1))
            self.items = (self.items | rng) if ctrl else rng
        elif ctrl:
            self.items ^= {i}
            self.anchor = i
        else:
            self.items = {i}
            self.anchor = i
        return before ^ self.items

    def select_all(self, n: int) -> set[int]:
        before = set(self.items)
        self.items = set(range(n))
        return before ^ self.items

    def clear(self) -> set[int]:
        before = set(self.items)
        self.items.clear()
        self.anchor = None
        return before

    def ordered(self) -> list[int]:
        return sorted(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __contains__(self, i: int) -> bool:
        return i in self.items


def common_tags(lists: list[list[str]]) -> list[tuple[str, int]]:
    """[(tag, number of images having it)] — on all images first, then by count, then name."""
    counts: dict[str, int] = {}
    for tags in lists:
        for t in tags:
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


# ── AI suggestions for several images ────────────────────────────────────────

@dataclass(frozen=True)
class Proposal:
    tag: str
    known: bool
    paths: tuple[Path, ...]          # images that would get the tag
    score: float                     # mean confidence over those images


def aggregate(scores: dict[Path, dict[str, float]], have: dict[Path, list[str]],
              store: TagStore | None, threshold: float, only_known: bool = False,
              limit: int = 60) -> list[Proposal]:
    """Suggestions for a selection: each tag with the images it would be added to
    (those where it scores above the threshold and is not there yet)."""
    by_tag: dict[str, list[tuple[Path, float]]] = {}
    known: dict[str, bool] = {}
    for path, sc in scores.items():
        for s in ai_tagger.suggest(sc, store, have.get(path, []), threshold, only_known):
            by_tag.setdefault(s.tag, []).append((path, s.score))
            known[s.tag] = s.known
    out = [Proposal(t, known[t], tuple(p for p, _ in hits), sum(x for _, x in hits) / len(hits))
           for t, hits in by_tag.items()]
    out.sort(key=lambda r: (-len(r.paths), -r.score, r.tag))
    return out[:limit]


# ── Thumbnails ───────────────────────────────────────────────────────────────

class ThumbStore:
    """JPEG previews (THUMB px) per image, stored in hoard.db. Each thread gets its own
    connection, so the loader thread and the UI never share one."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._local = threading.local()
        con = self._con()
        con.execute("""CREATE TABLE IF NOT EXISTS tagger_thumbs (
                           filepath TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL,
                           size INTEGER NOT NULL, jpg BLOB NOT NULL)""")
        con.commit()

    def _con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._local.con = sqlite3.connect(self._path, timeout=10)
        return con

    @staticmethod
    def render(path: Path) -> bytes:
        with Image.open(path) as im:
            im.draft("RGB", (THUMB * 2, THUMB * 2))
            rgba = im.convert("RGBA")
        flat = Image.alpha_composite(Image.new("RGBA", rgba.size, (40, 40, 48, 255)), rgba)
        rgb = flat.convert("RGB")
        rgb.thumbnail((THUMB, THUMB), Image.LANCZOS)
        buf = BytesIO()
        rgb.save(buf, "JPEG", quality=80)
        return buf.getvalue()

    def get(self, path: Path) -> bytes | None:
        row = self._con().execute(
            "SELECT mtime_ns, size, jpg FROM tagger_thumbs WHERE filepath=?",
            (str(path),)).fetchone()
        if row is None:
            return None
        try:
            st = path.stat()
        except OSError:
            return None
        return row[2] if (row[0], row[1]) == (st.st_mtime_ns, st.st_size) else None

    def make(self, path: Path) -> bytes:
        """Return the stored preview, creating it first when missing or out of date."""
        got = self.get(path)
        if got is not None:
            return got
        jpg = self.render(path)
        st = path.stat()
        con = self._con()
        con.execute("INSERT OR REPLACE INTO tagger_thumbs VALUES (?,?,?,?)",
                    (str(path), st.st_mtime_ns, st.st_size, jpg))
        con.commit()
        return jpg


class ThumbLoader:
    """Background thread that makes previews on request; `poll()` lists finished paths."""

    def __init__(self, store: ThumbStore) -> None:
        self._store = store
        self._todo: queue.LifoQueue[Path] = queue.LifoQueue()     # newest request first
        self._done: queue.Queue[tuple[Path, bytes | None]] = queue.Queue()
        self._wanted: set[Path] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def request(self, path: Path) -> None:
        with self._lock:
            if path in self._wanted:
                return
            self._wanted.add(path)
        self._todo.put(path)
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="tagger-thumbs", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            path = self._todo.get()
            try:
                jpg = self._store.make(path)
            except Exception as e:                  # unreadable image: show a placeholder
                log.debug("thumbnail failed for %s: %s", path, e)
                jpg = None
            with self._lock:
                self._wanted.discard(path)
            self._done.put((path, jpg))

    def poll(self) -> list[tuple[Path, bytes | None]]:
        out = []
        while True:
            try:
                out.append(self._done.get_nowait())
            except queue.Empty:
                return out


# ── AI jobs ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AIEvent:
    kind: str            # "download" | "model" | "progress" | "scored" | "error"
    done: int = 0
    total: int = 0
    message: str = ""
    paths: tuple[Path, ...] = ()


class AIJobs:
    """Runs the slow AI work off the UI thread. The UI calls `poll()` on a timer.

    `tagger` loads lazily; `scores_factory()` is called inside the worker thread and
    returns a ScoreStore (own connection)."""

    def __init__(self, tagger: ai_tagger.AITagger,
                 scores_factory: Callable[[], ai_tagger.ScoreStore],
                 downloader: Callable[..., None] = ai_tagger.download) -> None:
        self._tagger = tagger
        self.spec = tagger.spec
        self._scores_factory = scores_factory
        self._downloader = downloader
        self._events: queue.Queue[AIEvent] = queue.Queue()
        self._cancel = threading.Event()
        self._busy = False
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._busy

    def installed(self) -> bool:
        return ai_tagger.installed(self.spec)

    def cancel(self) -> None:
        self._cancel.set()

    def poll(self) -> list[AIEvent]:
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    def _start(self, target: Callable[[], None]) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._cancel.clear()
        threading.Thread(target=self._wrap, args=(target,), name="tagger-ai", daemon=True).start()
        return True

    def _wrap(self, target: Callable[[], None]) -> None:
        try:
            target()
        except ai_tagger.ModelError as e:
            self._events.put(AIEvent("error", message=str(e)))
        except Exception as e:
            log.exception("AI job failed")
            self._events.put(AIEvent("error", message=f"Unexpected error: {e}"))
        finally:
            self._busy = False

    def download(self) -> bool:
        def run() -> None:
            self._downloader(self.spec, progress=lambda d, t, n: self._events.put(
                AIEvent("download", d, t, n)), cancel=self._cancel.is_set)
            self._events.put(AIEvent("model", message="Model downloaded."))
        return self._start(run)

    def score(self, paths: list[Path]) -> bool:
        paths = list(paths)

        def run() -> None:
            cache = self._scores_factory()
            self._tagger.load()
            done, errors = ai_tagger.score_many(
                self._tagger, cache, paths,
                progress=lambda n, t: self._events.put(AIEvent("progress", n, t)),
                cancel=self._cancel.is_set)
            for path, err in errors[:1]:
                self._events.put(AIEvent("error", message=f"{Path(path).name}: {err}"))
            self._events.put(AIEvent("scored", done, len(paths), paths=tuple(paths)))
        return self._start(run)
