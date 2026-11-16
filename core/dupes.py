"""
core/dupes.py
-------------
Duplicate finder engine (no UI). Rebuilt from scratch.

    scan(folder)        -> index every media file under it (cached in SQLite)
    find_pairs(entries) -> every pair of files that look alike, with a % score
    build_groups(...)   -> review groups for a chosen similarity threshold

Why the old finder was wrong, and what this does differently
------------------------------------------------------------
* A file could be listed twice (two spellings of one path) and then matched
  with itself. Here every file has ONE identity (real path, case-normalised;
  hardlinks/symlinks to the same inode count once) and a pair is only ever
  built from two different identities.
* Groups were built by union-find, so A~B and B~C merged A and C even when they
  looked nothing alike. Here a group is a keeper plus files that each match
  THE KEEPER at the chosen threshold.
* The % score is real and re-usable: pairs are computed once down to a floor
  (default 80%), and moving the slider only re-groups, it never re-scans.

Fingerprint (images; GIF/WEBP use their first frame)
----------------------------------------------------
Each image is reduced to a 32x32 grey thumbnail. From it: a DCT perceptual hash
(pHash) and a gradient hash (dHash), for all 8 flips/rotations, so a mirrored
or rotated copy still matches. Two images match when
    score = 100 * (1 - mean(pHash distance, dHash distance) / 64)  >= threshold
AND their thumbnails correlate (guards against hash collisions) AND their
aspect ratios are compatible. Byte-identical files (sha256) score 100.

Limits (by design): heavy crops (more than ~10-15%) and big colour edits fall
below the correlation guard. Videos are matched by exact sha256 only for now.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
from PIL import Image, ImageOps

from core import videos

log = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".flv", ".m4v"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT
LOSSLESS_FMT = {"png", "bmp", "tiff", "tif"}

FLOOR = 80.0            # lowest similarity % ever computed (the slider's minimum)
NCC_MIN = 0.75          # thumbnails must correlate at least this much
ASPECT_TOL = 0.35       # relative aspect-ratio difference allowed
DUR_TOL = 0.03          # clips match only if their lengths differ by at most 3% (+0.5 s)
_THUMB = 32
PREVIEW_BOX = (96, 72)  # size of the stored JPEG preview used by the review list
_SCHEMA_VERSION = 1     # bump to invalidate cached fingerprints


# ── Data ──────────────────────────────────────────────────────────────────────

@dataclass
class Entry:
    key: str                       # identity: normalised real path
    path: str                      # path as found
    size: int
    mtime_ns: int
    sha256: str
    width: int = 0
    height: int = 0
    fmt: str = ""
    ph: np.ndarray | None = None   # (8,) uint64: pHash of the 8 orientations
    dh: np.ndarray | None = None   # (8,) uint64: dHash of the 8 orientations
    thumb: np.ndarray | None = None  # (32,32) uint8 grey
    tjpg: bytes | None = None      # small JPEG preview, only between fingerprinting and storing
    kind: str = "image"            # image | video | anim (animated GIF/WebP)
    duration: float = 0.0          # seconds (video, anim)
    fps: float = 0.0
    bitrate: int = 0               # bits per second (video)
    codec: str = ""
    frames: np.ndarray | None = None   # (8,32,32) uint8 grey frames over the clip
    fph: np.ndarray | None = None  # (8,) uint64 pHash of each frame
    fdh: np.ndarray | None = None  # (8,) uint64 dHash of each frame

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def has_fingerprint(self) -> bool:
        return self.thumb is not None

    @property
    def is_clip(self) -> bool:
        return self.kind in ("video", "anim")

    def set_frames(self, frames: np.ndarray) -> None:
        self.frames = frames
        self.fph = np.array([_phash(f) for f in frames], dtype=np.uint64)
        self.fdh = np.array([_dhash(f) for f in frames], dtype=np.uint64)


@dataclass(frozen=True)
class Pair:
    a: int                         # indexes into the entries list, a < b
    b: int
    score: float                   # 0-100
    exact: bool                    # byte-identical
    orient: int = 0                # which flip/rotation of b matched a (0 = none)


@dataclass
class Group:
    members: list[int]             # entry indexes, keeper first
    scores: dict[int, float]       # member -> score against the keeper
    exact: bool                    # every member byte-identical to the keeper

    @property
    def keeper(self) -> int:
        return self.members[0]


@dataclass
class ScanReport:
    entries: list[Entry] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    reused: int = 0
    computed: int = 0
    cancelled: bool = False
    notes: list[str] = field(default_factory=list)      # things the user should know about


# ── Identity ──────────────────────────────────────────────────────────────────

def file_key(path: str | Path) -> str:
    """The identity of a file: same file -> same key, however the path is spelled."""
    return os.path.normcase(os.path.realpath(path))


def iter_media(folder: str | Path) -> list[Path]:
    """All media files below `folder`, each real file once."""
    out: list[Path] = []
    seen_keys: set[str] = set()
    seen_inodes: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.suffix.lower() not in MEDIA_EXT:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            key = file_key(p)
            if key in seen_keys:
                continue
            ino = (st.st_dev, st.st_ino)
            if st.st_ino and ino in seen_inodes:      # hardlink to a file already listed
                continue
            seen_keys.add(key)
            if st.st_ino:
                seen_inodes.add(ino)
            out.append(p)
    return out


# ── Fingerprinting ────────────────────────────────────────────────────────────

def _dct_matrix(n: int = _THUMB) -> np.ndarray:
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    return np.cos(np.pi * (2 * i + 1) * k / (2 * n))


_DCT = _dct_matrix()


def _pack(bits: np.ndarray) -> int:
    return int.from_bytes(np.packbits(bits.astype(np.uint8).ravel()).tobytes(), "big")


def _phash(g: np.ndarray) -> int:
    coef = _DCT @ g.astype(np.float64) @ _DCT.T
    low = coef[:8, :8].ravel()
    bits = low > np.median(low[1:])
    bits[0] = False                                    # brightness is not content
    return _pack(bits)


def _dhash(g: np.ndarray) -> int:
    small = Image.fromarray(g.astype(np.float32), mode="F").resize((9, 8), Image.BOX)
    a = np.asarray(small)
    return _pack(a[:, 1:] > a[:, :-1])


def orientations(g: np.ndarray) -> list[np.ndarray]:
    """The 8 flips/rotations of a square array. Index 0 is the identity."""
    t = g.T
    return [g, g[:, ::-1], g[::-1, :], g[::-1, ::-1],
            t, t[:, ::-1], t[::-1, :], t[::-1, ::-1]]


def _grey_thumb(path: Path) -> tuple[np.ndarray, int, int, str, bytes]:
    """-> (32x32 uint8 grey, width, height after EXIF rotation, format, JPEG preview)."""
    with Image.open(path) as im:
        fmt = (im.format or path.suffix.lstrip(".")).lower()
        w, h = im.size
        orient = im.getexif().get(274, 1)
        if orient in (5, 6, 7, 8):
            w, h = h, w
        if fmt in ("jpeg", "jpg"):
            im.draft("L", (_THUMB * 4, _THUMB * 4))      # decode small: much faster
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, rgba)
        small = im.convert("RGB")
        small.thumbnail(PREVIEW_BOX)
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=78)
        g = im.convert("L").resize((_THUMB, _THUMB), Image.LANCZOS)
        return np.asarray(g, dtype=np.uint8).copy(), w, h, fmt, buf.getvalue()


def fingerprint(path: Path):
    """-> (pHashes, dHashes, 32x32 grey, width, height, format, JPEG preview)."""
    g, w, h, fmt, jpg = _grey_thumb(path)
    variants = orientations(g)
    ph = np.array([_phash(v) for v in variants], dtype=np.uint64)
    dh = np.array([_dhash(v) for v in variants], dtype=np.uint64)
    return ph, dh, g, w, h, fmt, jpg


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


# ── Cache ─────────────────────────────────────────────────────────────────────

class DupeIndex:
    """Fingerprint cache in SQLite, keyed by file identity + size + mtime."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._db = conn
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS dupe_index (
                    key       TEXT PRIMARY KEY,
                    path      TEXT NOT NULL,
                    size      INTEGER NOT NULL,
                    mtime_ns  INTEGER NOT NULL,
                    sha256    TEXT NOT NULL,
                    width     INTEGER NOT NULL DEFAULT 0,
                    height    INTEGER NOT NULL DEFAULT 0,
                    fmt       TEXT NOT NULL DEFAULT '',
                    ph        BLOB, dh BLOB, thumb BLOB,
                    version   INTEGER NOT NULL
                )""")
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(dupe_index)")}
            for name, decl in (("tjpg", "BLOB"), ("kind", "TEXT NOT NULL DEFAULT 'image'"),
                               ("duration", "REAL NOT NULL DEFAULT 0"),
                               ("fps", "REAL NOT NULL DEFAULT 0"),
                               ("bitrate", "INTEGER NOT NULL DEFAULT 0"),
                               ("codec", "TEXT NOT NULL DEFAULT ''"), ("frames", "BLOB")):
                if name not in cols:                # older index: add the new columns
                    self._db.execute(f"ALTER TABLE dupe_index ADD COLUMN {name} {decl}")

    @staticmethod
    def read_preview(conn: sqlite3.Connection, key: str) -> bytes | None:
        """The stored JPEG preview of a file, if any (used by the review list)."""
        r = conn.execute("SELECT tjpg FROM dupe_index WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def _load(self, key: str, size: int, mtime_ns: int) -> Entry | None:
        r = self._db.execute(
            "SELECT path,size,mtime_ns,sha256,width,height,fmt,ph,dh,thumb,version,"
            "tjpg IS NOT NULL,kind,duration,fps,bitrate,codec,frames "
            "FROM dupe_index WHERE key=?", (key,)).fetchone()
        if not r or r[1] != size or r[2] != mtime_ns or r[10] != _SCHEMA_VERSION:
            return None
        ph = np.frombuffer(r[7], dtype="<u8").astype(np.uint64) if r[7] else None
        dh = np.frombuffer(r[8], dtype="<u8").astype(np.uint64) if r[8] else None
        th = np.frombuffer(r[9], dtype=np.uint8).reshape(_THUMB, _THUMB).copy() if r[9] else None
        e = Entry(key, r[0], size, mtime_ns, r[3], r[4], r[5], r[6], ph, dh, th)
        e.has_preview = bool(r[11])
        e.kind, e.duration, e.fps, e.bitrate, e.codec = r[12], r[13], r[14], r[15], r[16]
        if r[17]:
            e.set_frames(np.frombuffer(r[17], dtype=np.uint8).reshape(-1, _THUMB, _THUMB).copy())
        return e

    def _store(self, e: Entry) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO dupe_index (key,path,size,mtime_ns,sha256,width,height,"
                "fmt,ph,dh,thumb,version,tjpg,kind,duration,fps,bitrate,codec,frames) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (e.key, e.path, e.size, e.mtime_ns, e.sha256, e.width, e.height, e.fmt,
                 e.ph.astype("<u8").tobytes() if e.ph is not None else None,
                 e.dh.astype("<u8").tobytes() if e.dh is not None else None,
                 e.thumb.tobytes() if e.thumb is not None else None,
                 _SCHEMA_VERSION, e.tjpg, e.kind, e.duration, e.fps, e.bitrate, e.codec,
                 e.frames.tobytes() if e.frames is not None else None))
        e.tjpg = None                                # keep memory small

    def _update_clip(self, e: Entry) -> None:
        with self._db:
            self._db.execute(
                "UPDATE dupe_index SET kind=?,duration=?,fps=?,bitrate=?,codec=?,frames=? WHERE key=?",
                (e.kind, e.duration, e.fps, e.bitrate, e.codec,
                 e.frames.tobytes() if e.frames is not None else None, e.key))

    @staticmethod
    def _fill_animation(e: Entry, p: Path) -> bool:
        """Animated GIF/WebP: also fingerprint the whole animation (Pillow only)."""
        if not videos.is_animated(p):
            return False
        s = videos.animation_frames(p)
        e.kind, e.duration, e.fps = "anim", s.info.duration, s.info.fps
        e.set_frames(s.frames)
        return True

    @staticmethod
    def _fill_video(e: Entry, p: Path, rep: "ScanReport") -> None:
        if not videos.available():
            note = "ffmpeg/ffprobe not found: videos are matched only when byte-identical."
            if note not in rep.notes:
                rep.notes.append(note)
            return
        try:
            s = videos.sample_frames(p)
        except videos.VideoError as err:
            rep.errors.append((str(p), f"not fingerprinted: {err}"))
            return
        e.kind, e.width, e.height = "video", s.info.width, s.info.height
        e.duration, e.fps, e.bitrate = s.info.duration, s.info.fps, s.info.bitrate
        e.codec = s.info.vcodec
        e.tjpg = s.poster
        e.set_frames(s.frames)

    def scan(self, folder: str | Path,
             progress: Callable[[int, int, str], None] | None = None,
             cancel: Callable[[], bool] | None = None,
             include_clips: bool = True) -> ScanReport:
        """Index every media file under `folder` (blocking; run it in a thread).

        include_clips=False leaves videos out (they are slower: ffmpeg reads 8 frames each).
        """
        files = [f for f in iter_media(folder) if include_clips or f.suffix.lower() not in VIDEO_EXT]
        rep = ScanReport()
        for n, p in enumerate(files, 1):
            if cancel and cancel():
                rep.cancelled = True
                break
            if progress:
                progress(n, len(files), str(p))
            try:
                st = p.stat()
                key = file_key(p)
                ext = p.suffix.lower()
                e = self._load(key, st.st_size, st.st_mtime_ns)
                if e is not None:
                    e.path = str(p)
                    rep.reused += 1
                    if not getattr(e, "has_preview", True) and ext in IMAGE_EXT:
                        try:                         # index from before previews existed
                            jpg = _grey_thumb(p)[4]
                            with self._db:
                                self._db.execute("UPDATE dupe_index SET tjpg=? WHERE key=?",
                                                 (jpg, key))
                        except Exception as err:
                            rep.errors.append((str(p), f"no preview: {err}"))
                    if ext in (".gif", ".webp") and e.frames is None and e.kind == "image":
                        try:                         # indexed before animations were understood
                            if self._fill_animation(e, p):
                                self._update_clip(e)
                        except Exception as err:
                            rep.errors.append((str(p), f"animation not read: {err}"))
                    if ext in VIDEO_EXT and e.frames is None and videos.available() and e.kind == "image":
                        self._fill_video(e, p, rep)  # indexed before ffmpeg was installed
                        if e.frames is not None:
                            self._update_clip(e)
                            with self._db:
                                self._db.execute("UPDATE dupe_index SET tjpg=?,width=?,height=? "
                                                 "WHERE key=?", (e.tjpg, e.width, e.height, key))
                            e.tjpg = None
                else:
                    e = Entry(key, str(p), st.st_size, st.st_mtime_ns, sha256_of(p),
                              fmt=ext.lstrip("."))
                    if ext in IMAGE_EXT:
                        try:
                            (e.ph, e.dh, e.thumb, e.width, e.height, e.fmt,
                             e.tjpg) = fingerprint(p)
                            if ext in (".gif", ".webp"):
                                self._fill_animation(e, p)
                        except Exception as err:             # unreadable image: exact-only
                            rep.errors.append((str(p), f"not fingerprinted: {err}"))
                    elif ext in VIDEO_EXT:
                        self._fill_video(e, p, rep)
                    self._store(e)
                    rep.computed += 1
                if e.kind == "image" and ext in VIDEO_EXT and not videos.available():
                    msg = "ffmpeg/ffprobe not found: videos are matched only when byte-identical."
                    if msg not in rep.notes:
                        rep.notes.append(msg)
                rep.entries.append(e)
            except OSError as err:
                rep.errors.append((str(p), str(err)))
        return rep


# ── Matching ──────────────────────────────────────────────────────────────────

def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    x = a.astype(np.float64).ravel()
    y = b.astype(np.float64).ravel()
    x -= x.mean()
    y -= y.mean()
    den = np.linalg.norm(x) * np.linalg.norm(y)
    if den < 1e-6:                                    # flat image(s)
        return 1.0 if abs(float(a.mean()) - float(b.mean())) < 8 else 0.0
    return float(x @ y / den)


def _aspect_ok(a: Entry, b: Entry, orient: int) -> bool:
    if not (a.width and a.height and b.width and b.height):
        return True
    ra = a.width / a.height
    rb = b.width / b.height
    if orient >= 4:                                   # b was transposed to match a
        rb = 1 / rb
    return abs(ra - rb) / max(ra, rb) <= ASPECT_TOL


def find_pairs(entries: list[Entry], floor: float = FLOOR,
               ignored: Iterable[tuple[str, str]] = (),
               transforms: bool = True,
               progress: Callable[[int, int], None] | None = None) -> list[Pair]:
    """Every pair scoring at least `floor` (0-100). Never pairs a file with itself."""
    skip = {tuple(sorted(p)) for p in ignored}
    pairs: dict[tuple[int, int], Pair] = {}

    def allowed(i: int, j: int) -> bool:
        return i != j and entries[i].key != entries[j].key and \
            tuple(sorted((entries[i].key, entries[j].key))) not in skip

    # 1. byte-identical
    by_sha: dict[str, list[int]] = {}
    for i, e in enumerate(entries):
        if e.sha256:
            by_sha.setdefault(e.sha256, []).append(i)
    for idx in by_sha.values():
        for x in range(len(idx)):
            for y in range(x + 1, len(idx)):
                i, j = idx[x], idx[y]
                if allowed(i, j):
                    pairs[(i, j)] = Pair(i, j, 100.0, True)

    # 2. perceptual
    cand = [i for i, e in enumerate(entries) if e.has_fingerprint]
    n = len(cand)
    if n >= 2:
        P = np.stack([entries[i].ph for i in cand])          # (n, 8) uint64
        base = P[:, 0]
        max_bits = (100.0 - floor) / 100.0 * 64.0
        cand_max = int(max_bits) + 4                          # slack: score averages 2 hashes
        chunk = max(1, int(4_000_000 // (n * 8)))
        for s in range(0, n, chunk):
            x = base[s:s + chunk, None, None] ^ P[None, :, :]
            d = np.bitwise_count(x)
            if not transforms:
                d = d[:, :, :1]
            best = d.argmin(axis=2)
            dmin = d.min(axis=2)
            rows, cols = np.nonzero(dmin <= cand_max)
            for r, c in zip(rows.tolist(), cols.tolist()):
                ci, cj = s + r, c
                if cj <= ci:
                    continue
                i, j = cand[ci], cand[cj]
                if (i, j) in pairs or not allowed(i, j):
                    continue
                o = int(best[r, c])
                a, b = entries[i], entries[j]
                dd = int(np.bitwise_count(a.dh[0] ^ b.dh[o]))
                bits = (int(dmin[r, c]) + dd) / 2.0
                score = 100.0 * (1.0 - bits / 64.0)
                if score < floor or not _aspect_ok(a, b, o):
                    continue
                if _ncc(a.thumb, orientations(b.thumb)[o]) < NCC_MIN:
                    continue
                pairs[(i, j)] = Pair(i, j, round(score, 1), False, o)
            if progress:
                progress(min(s + chunk, n), n)
    _clip_pairs(entries, floor, allowed, pairs)
    return sorted(pairs.values(), key=lambda p: (-p.score, p.a, p.b))


def _clip_score(a: Entry, b: Entry) -> float:
    """0-100 similarity of two clips: frame by frame at the same fractions of the clip."""
    bits = (np.bitwise_count(a.fph ^ b.fph).astype(np.float64) +
            np.bitwise_count(a.fdh ^ b.fdh)) / 2.0
    return float(100.0 * (1.0 - bits.mean() / 64.0))


def _clip_pairs(entries: list[Entry], floor: float, allowed, pairs: dict) -> None:
    """Videos / animations of (almost) the same length whose frames look alike."""
    idx = sorted((i for i, e in enumerate(entries) if e.frames is not None),
                 key=lambda i: entries[i].duration)
    if len(idx) < 2:
        return
    dur = np.array([entries[i].duration for i in idx])
    ph = np.stack([entries[i].fph for i in idx])               # (n, 8)
    cand_max = (100.0 - floor) / 100.0 * 64.0 + 4              # mean bits per frame
    for x, i in enumerate(idx):
        a = entries[i]
        hi = int(np.searchsorted(dur, a.duration * (1 + DUR_TOL) + 0.5, side="right"))
        if hi <= x + 1:
            continue
        window = slice(x + 1, hi)
        mean_bits = np.bitwise_count(ph[window] ^ ph[x]).mean(axis=1)
        for off in np.nonzero(mean_bits <= cand_max)[0].tolist():
            j = idx[x + 1 + off]
            b = entries[j]
            key = (min(i, j), max(i, j))
            if key in pairs or not allowed(i, j) or not _aspect_ok(a, b, 0):
                continue
            score = _clip_score(a, b)
            if score < floor:
                continue
            ncc = float(np.median([_ncc(fa, fb) for fa, fb in zip(a.frames, b.frames)]))
            if ncc < NCC_MIN:
                continue
            pairs[key] = Pair(key[0], key[1], round(score, 1), False, 0)


# ── Keeper choice and grouping ────────────────────────────────────────────────

def rank_key(e: Entry) -> tuple:
    """Smaller = better copy: resolution, lossless, longer clip, bitrate, size, older, name."""
    return (-e.pixels, 0 if e.fmt in LOSSLESS_FMT else 1, -round(e.duration), -e.bitrate,
            -e.size, e.mtime_ns, e.key)


def keeper_reason(keeper: Entry, other: Entry) -> str:
    """Why `other` is the one to skip, in a few words."""
    if keeper.pixels != other.pixels:
        return (f"lower resolution ({other.width}×{other.height} "
                f"vs {keeper.width}×{keeper.height})")
    if (keeper.fmt in LOSSLESS_FMT) != (other.fmt in LOSSLESS_FMT):
        return "lossy format vs lossless"
    if round(keeper.duration) != round(other.duration):
        return "shorter clip"
    if keeper.bitrate != other.bitrate:
        return "lower bitrate"
    if keeper.size != other.size:
        return "smaller file" if other.size < keeper.size else "larger file for the same picture"
    if keeper.mtime_ns != other.mtime_ns:
        return "newer copy"
    return "identical copy"


def build_groups(entries: list[Entry], pairs: list[Pair], threshold: float) -> list[Group]:
    """Groups at `threshold` %: a keeper plus the files that match the keeper."""
    adj: dict[int, dict[int, Pair]] = {}
    for p in pairs:
        if p.score >= threshold:
            adj.setdefault(p.a, {})[p.b] = p
            adj.setdefault(p.b, {})[p.a] = p
    order = sorted(adj, key=lambda i: rank_key(entries[i]))
    taken: set[int] = set()
    groups: list[Group] = []
    for seed in order:
        if seed in taken:
            continue
        others = [j for j in adj[seed] if j not in taken]
        if not others:
            continue
        others.sort(key=lambda j: rank_key(entries[j]))
        scores = {j: adj[seed][j].score for j in others}
        groups.append(Group([seed] + others, scores,
                            all(adj[seed][j].exact for j in others)))
        taken.add(seed)
        taken.update(others)
    return groups


def artist_of(path: str | Path, roots: "str | Path | list[str | Path]") -> str:
    """Artist = the first folder below a root, in the layout  root / artist / site / file.

    `roots` is one root or a list tried in order (the download folder first, then the
    scanned folder). The first root that contains the file in a sub-folder wins; ''
    if none does.
    """
    if isinstance(roots, (str, Path)):
        roots = [roots]
    target = Path(path).resolve()
    for root in roots:
        if not root:
            continue
        try:
            parts = target.relative_to(Path(root).resolve()).parts
        except ValueError:
            continue
        if len(parts) > 1:
            return parts[0]
    return ""
