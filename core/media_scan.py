"""
core/media_scan.py
-------------------
Shared scanning/hashing logic used by the Tag Media and Find Duplicates
tabs. Decoupled from the UI — takes plain paths, returns plain data.

Media types
-----------
  Images (.jpg/.jpeg/.png/.gif/.webp/.bmp) — opened directly with Pillow.
  Animated GIF/WEBP are handled fine as-is; Pillow just reads frame 0 by
  default for hashing/thumbnailing purposes, which is all dedup needs.

  Videos (.mp4/.webm/.mov/.mkv/.avi/.flv/.m4v) — Pillow can't open these
  at all. A representative frame is extracted with ffmpeg (a system
  binary, NOT a Python dependency) and hashed/thumbnailed the same way
  an image would be. If ffmpeg/ffprobe aren't installed on this machine,
  video files are still picked up by sha256 (exact-duplicate detection
  still works) but get no phash (near-duplicate detection is skipped for
  them) and no thumbnail — see ffmpeg_available().

Hashing
-------
Two hashes are computed per file and cached in artists_media (file_db.py):

  sha256  — exact byte-identical duplicate detection. Cheap, no extra
            dependency, zero false positives. Computed on the raw file
            bytes for BOTH images and videos.
  phash   — perceptual hash (via the 'imagehash' package) for catching
            near-duplicates: re-saves, resizes, recompresses, minor crops.
            For videos this is computed on one extracted frame, not the
            video stream itself — two videos with different audio/encoding
            but the same visual content at the sampled timestamp will
            still phash close together.

Both are stored so re-running a scan only has to hash files that are new
or have changed size since the last pass.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from core.file_db import FileDB

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".flv", ".m4v"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# Hamming distance threshold below which two phashes are considered
# "near-duplicate". 0 = identical perceptual hash. Empirically, <= 8
# (out of 64 bits for the default 8x8 phash) catches resizes/recompresses
# while staying well clear of merely-similar-but-different images.
DEFAULT_PHASH_THRESHOLD = 8

# Sample the frame at 10% into the clip rather than frame 0 — many videos
# open on a black/loading/title frame that hashes nothing like the actual
# content, which would make every video phash collide on "black frame".
VIDEO_SAMPLE_FRACTION = 0.10


@lru_cache(maxsize=1)
def ffmpeg_available() -> bool:
    """
    True if both ffmpeg and ffprobe are on PATH. Cached — checking PATH
    is cheap but there's no reason to call shutil.which() repeatedly for
    every single video file in a scan.
    """
    available = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
    log.debug("ffmpeg_available() = %s", available)
    return available


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def get_video_duration(path: Path) -> float:
    """Duration in seconds, or 0.0 if ffprobe can't read it."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        duration = float(result.stdout.strip())
        log.debug("get_video_duration(%s) = %.2fs", path.name, duration)
        return duration
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        log.debug("get_video_duration(%s) failed: %s", path.name, e)
        return 0.0


def extract_video_frame(path: Path, timestamp: float | None = None) -> Path | None:
    """
    Extract one frame from *path* as a temporary PNG and return its path,
    or None if ffmpeg is unavailable or extraction fails. Caller is
    responsible for deleting the returned temp file when done with it.
    """
    if not ffmpeg_available():
        log.debug("extract_video_frame(%s): skipped, ffmpeg not available", path.name)
        return None

    if timestamp is None:
        duration = get_video_duration(path)
        timestamp = duration * VIDEO_SAMPLE_FRACTION if duration > 0 else 0.0

    fd, out_path_str = tempfile.mkstemp(suffix=".png")
    import os
    os.close(fd)
    out_path = Path(out_path_str)

    log.debug("extract_video_frame(%s) at t=%.2fs -> %s", path.name, timestamp, out_path)
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(timestamp), "-i", str(path),
             "-vframes", "1", str(out_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            log.debug("extract_video_frame(%s): ffmpeg failed (code=%s)",
                      path.name, result.returncode)
            out_path.unlink(missing_ok=True)
            return None
        return out_path
    except (subprocess.SubprocessError, OSError) as e:
        log.debug("extract_video_frame(%s): exception: %s", path.name, e)
        out_path.unlink(missing_ok=True)
        return None


def iter_media_files(folder: str | Path) -> list[Path]:
    """
    All image AND video files under *folder*, recursively, sorted for
    stable ordering.

    Returns resolved (canonical, absolute) paths — not whatever relative
    or differently-spelled form *folder* happened to be passed in as.
    This matters: if the same physical file were registered once under
    a relative path and once under its absolute path (e.g. scanning
    "./download" in one session and an absolute path in another), it
    would get two different DB rows with an identical hash and show up
    as "duplicate of itself" even though only one file actually exists.
    """
    folder = Path(folder)
    if not folder.exists():
        log.debug("iter_media_files: folder does not exist: %s", folder)
        return []
    files = sorted(
        p.resolve() for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS
    )
    log.debug("iter_media_files: found %d file(s) under %s", len(files), folder)
    return files


# Back-compat alias — existing callers (tag_media_tab.py, duplicates_tab.py)
# used this name when only images were supported. Now returns images AND
# videos; kept under the old name so nothing else needs to change.
iter_image_files = iter_media_files


def compute_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """Stream the file in chunks so large images don't blow up memory."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_phash(path: Path) -> str | None:
    """
    Perceptual hash as a hex string, or None if:
      - the file can't be read as an image (corrupt file, unsupported format)
      - Pillow/imagehash aren't installed
      - it's a video and ffmpeg isn't available to extract a frame
      - frame extraction otherwise fails (corrupt/unreadable video)
    """
    try:
        from PIL import Image
        import imagehash
    except ImportError:
        return None

    if is_video(path):
        frame_path = extract_video_frame(path)
        if frame_path is None:
            return None
        try:
            with Image.open(frame_path) as img:
                return str(imagehash.phash(img))
        except Exception:
            return None
        finally:
            frame_path.unlink(missing_ok=True)

    try:
        with Image.open(path) as img:
            return str(imagehash.phash(img))
    except Exception:
        return None


def open_thumbnail_image(path: Path, size: tuple[int, int] = (320, 320)):
    """
    Return a Pillow Image thumbnail for *path*, or None if it can't be
    produced (missing Pillow, corrupt file, or — for video — no ffmpeg
    available to extract a frame).

    Works for both static/animated images and video files, so UI code
    doesn't need to branch on file type before generating a preview.
    Caller converts the result to ImageTk.PhotoImage as needed; this
    module stays UI-toolkit-agnostic.
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    if is_video(path):
        frame_path = extract_video_frame(path)
        if frame_path is None:
            return None
        try:
            img = Image.open(frame_path)
            img.load()              # force read before the temp file is deleted
            img.thumbnail(size)
            return img
        except Exception:
            return None
        finally:
            frame_path.unlink(missing_ok=True)

    try:
        img = Image.open(path)
        img.thumbnail(size)
        return img
    except Exception:
        return None


@dataclass
class HashResult:
    path: Path
    sha256: str | None = None
    phash: str | None = None
    error: str | None = None


def hash_file(path: Path) -> HashResult:
    """Compute both hashes for one file. Never raises — errors are captured."""
    try:
        sha = compute_sha256(path)
    except OSError as e:
        return HashResult(path=path, error=f"Could not read file: {e}")
    phash = compute_phash(path)
    return HashResult(path=path, sha256=sha, phash=phash)


def needs_hashing(db: FileDB, path: Path) -> bool:
    """
    True if this file has no cached hash yet, or its size on disk no
    longer matches what's recorded — i.e. it changed since last hashed.
    """
    row = db.get(str(path))
    if row is None or not row["sha256"]:
        return True
    try:
        current_size = path.stat().st_size
    except OSError:
        return True
    return row["file_size"] != current_size


def hash_and_store(db: FileDB, path: Path) -> HashResult:
    """Hash *path* and persist the result (and file_size) into *db*."""
    result = hash_file(path)
    if result.error:
        log.warning("hash_and_store: %s failed: %s", path, result.error)
        return result
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if not db.get(str(path)):
        db.register(filename=path.name, filepath=str(path), file_size=size)
    else:
        db._con.execute(
            "UPDATE artists_media SET file_size = ? WHERE filepath = ?",
            (size, str(path)),
        )
        db._con.commit()
    db.set_hashes(str(path), sha256=result.sha256 or "", phash=result.phash or "")
    return result


def sync_tags(db: FileDB, base: Path, image_path: Path, tags: list[str]) -> Path:
    """
    Write *tags* for *image_path* to both the .txt sidecar (so the
    Uploader tab picks them up unchanged) and the database (so this app's
    own tag editor/search can read them back without touching disk).

    Returns the sidecar file path that was written.
    """
    from core.upload_data import write_tag_file
    sidecar_path = write_tag_file(image_path, base, tags)
    db.set_tags(str(image_path), tags)
    log.debug("sync_tags: %s -> %s (tags=%s)", image_path.name, sidecar_path, tags)
    return sidecar_path


# ── Duplicate grouping ──────────────────────────────────────────────────────────

@dataclass
class DuplicateGroup:
    kind: str                       # "exact" | "near"
    paths: list[Path] = field(default_factory=list)
    distance: int = 0               # 0 for exact groups; max pairwise Hamming distance for near groups


def _hamming(a: str, b: str) -> int:
    """Hamming distance between two equal-length hex hash strings."""
    try:
        ia, ib = int(a, 16), int(b, 16)
    except ValueError:
        return 999
    return bin(ia ^ ib).count("1")


def find_exact_duplicate_groups(db: FileDB) -> list[DuplicateGroup]:
    """
    Group files sharing an identical sha256, skipping rows whose file
    vanished, and collapsing rows that point at the same physical file
    (e.g. registered once under a relative path, once under its
    absolute path) so a single real file never appears as "duplicate
    of itself".
    """
    groups: list[DuplicateGroup] = []
    for sha, rows in db.find_exact_duplicates().items():
        seen_real_paths: dict[str, Path] = {}
        for r in rows:
            p = Path(r["filepath"])
            if not p.exists():
                continue
            real = str(p.resolve())
            seen_real_paths.setdefault(real, p.resolve())
        paths = list(seen_real_paths.values())
        if len(paths) > 1:
            groups.append(DuplicateGroup(kind="exact", paths=paths, distance=0))
    log.debug("find_exact_duplicate_groups: %d group(s) found", len(groups))
    return groups


def find_near_duplicate_groups(
    db: FileDB,
    threshold: int = DEFAULT_PHASH_THRESHOLD,
    exclude_exact: bool = True,
) -> list[DuplicateGroup]:
    """
    Group files whose phash Hamming distance is <= threshold.

    Naive O(n^2) comparison — fine for personal-hoard sizes (thousands,
    not millions, of images). If exclude_exact is True, pairs that are
    already exact sha256 duplicates are skipped (they're reported by
    find_exact_duplicate_groups instead, so a pair isn't shown twice).
    """
    rows = [r for r in db.all_hashed() if r["phash"] and Path(r["filepath"]).exists()]

    # Collapse rows that point at the same physical file (see the docstring
    # note in find_exact_duplicate_groups) — without this, the very same
    # file registered under two path spellings would phash-match itself
    # at distance 0 and appear as its own near-duplicate.
    seen_real_paths: dict[str, object] = {}
    for r in rows:
        real = str(Path(r["filepath"]).resolve())
        seen_real_paths.setdefault(real, r)
    rows = list(seen_real_paths.values())

    n = len(rows)
    visited = [False] * n
    groups: list[DuplicateGroup] = []

    for i in range(n):
        if visited[i]:
            continue
        cluster = [i]
        for j in range(i + 1, n):
            if visited[j]:
                continue
            if exclude_exact and rows[i]["sha256"] == rows[j]["sha256"]:
                continue   # already an exact duplicate, reported elsewhere
            dist = _hamming(rows[i]["phash"], rows[j]["phash"])
            if dist <= threshold:
                cluster.append(j)
        if len(cluster) > 1:
            for idx in cluster:
                visited[idx] = True
            max_dist = max(
                (_hamming(rows[a]["phash"], rows[b]["phash"])
                 for a in cluster for b in cluster if a != b),
                default=0,
            )
            groups.append(DuplicateGroup(
                kind="near",
                paths=[Path(rows[idx]["filepath"]).resolve() for idx in cluster],
                distance=max_dist,
            ))

    log.debug("find_near_duplicate_groups: threshold=%d -> %d group(s) found",
              threshold, len(groups))
    return groups