"""
core/dupes_meta.py
------------------
Pure logic behind the duplicate comparison window (no Tk):

  * common_root / split_paths   shared folder shown once, then ./relative paths
  * compare_rows                metadata grid rows, flagged same (green) / different (orange)
  * View, view_box              mirrored zoom/pan that stays aligned across images
                                of different resolutions
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from core.dupes import Entry, artist_of, site_of


# ── Paths ─────────────────────────────────────────────────────────────────────

def _slash(p: str) -> str:
    return p.replace("\\", "/")


def split_paths(a: str, b: str) -> tuple[str, str, str]:
    """('/foo/', './bar/zoom.jpg', './barr/zim.jpg') for foo/bar/zoom.jpg and foo/barr/zim.jpg.

    The root is cut at a folder boundary, never inside a name. With no shared
    folder (e.g. two drives) the root is '' and full paths are returned.
    """
    pa, pb = os.path.abspath(a), os.path.abspath(b)
    try:
        root = os.path.commonpath([os.path.dirname(pa), os.path.dirname(pb)])
    except ValueError:
        return "", _slash(pa), _slash(pb)
    rel = lambda p: "./" + _slash(os.path.relpath(p, root))
    shown = _slash(root)
    return (shown if shown.endswith("/") else shown + "/"), rel(pa), rel(pb)


# ── Metadata grid ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Row:
    label: str
    left: str
    right: str
    same: bool | None          # True green, False orange, None neutral
    worse: str = ""            # "left" / "right": the smaller side, shown in red


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            txt = f"{int(size)} {unit}" if unit == "B" else f"{size:.2f} {unit}"
            return f"{txt} · {n:,} B" if unit != "B" else txt
        size /= 1024
    return str(n)


def _when(e: Entry) -> datetime:
    return datetime.fromtimestamp(e.mtime_ns / 1e9)


def compare_rows(a: Entry, b: Entry, scan_root: "str | list[str]" = "",
                 score: float | None = None) -> tuple[str, list[Row]]:
    """-> (shared root to print above the grid, rows). Size is exact, date is per day."""
    root, ra, rb = split_paths(a.path, b.path)
    da, db = _when(a), _when(b)
    art_a = artist_of(a.path, scan_root) if scan_root else ""
    art_b = artist_of(b.path, scan_root) if scan_root else ""
    site_a = site_of(a.path, scan_root) if scan_root else ""
    site_b = site_of(b.path, scan_root) if scan_root else ""
    asp = lambda e: f"{e.width / e.height:.2f}" if e.width and e.height else "—"
    res = lambda e: f"{e.width}×{e.height}" if e.width and e.height else "—"
    def smaller(x: int, y: int) -> str:
        return "" if x == y else ("left" if x < y else "right")
    small_px = smaller(a.pixels, b.pixels) if a.width and a.height and b.width and b.height else ""
    rows = [
        Row("Path", ra, rb, False if ra != rb else True),
        Row("Date", da.strftime("%Y-%m-%d %H:%M"), db.strftime("%Y-%m-%d %H:%M"),
            da.date() == db.date()),
        Row("Size", human_size(a.size), human_size(b.size), a.size == b.size, smaller(a.size, b.size)),
        Row("Resolution", res(a), res(b), (a.width, a.height) == (b.width, b.height), small_px),
        Row("Artist", art_a or "—", art_b or "—", art_a == art_b),
        Row("Site", site_a or "—", site_b or "—", site_a == site_b),
        Row("Format", a.fmt.upper() or "—", b.fmt.upper() or "—", a.fmt == b.fmt),
        Row("Aspect", asp(a), asp(b), asp(a) == asp(b), small_px if asp(a) != asp(b) else ""),
        Row("Bytes", a.sha256[:10], b.sha256[:10], a.sha256 == b.sha256),
    ]
    if a.is_clip or b.is_clip:
        dur = lambda e: f"{int(e.duration // 60)}:{e.duration % 60:04.1f}" if e.duration else "—"
        rows += [
            Row("Duration", dur(a), dur(b), abs(a.duration - b.duration) < 0.5),
            Row("Frame rate", f"{a.fps:.2f}" if a.fps else "—", f"{b.fps:.2f}" if b.fps else "—",
                round(a.fps, 1) == round(b.fps, 1)),
            Row("Bitrate", f"{a.bitrate // 1000} kb/s" if a.bitrate else "—",
                f"{b.bitrate // 1000} kb/s" if b.bitrate else "—", a.bitrate == b.bitrate),
            Row("Codec", a.codec or "—", b.codec or "—", a.codec == b.codec),
        ]
    if score is not None:
        rows.append(Row("Match", "", f"{score:.1f}%", None))
    return root, rows


# ── Mirrored zoom / pan ───────────────────────────────────────────────────────

MIN_ZOOM, MAX_ZOOM = 0.1, 64.0


@dataclass
class View:
    """Shared by both panes. zoom 1.0 = each image fits its own pane; (cx, cy) is the
    centre of the visible area as a fraction of the image, so the same picture region
    is shown on both sides whatever their resolution."""
    zoom: float = 1.0
    cx: float = 0.5
    cy: float = 0.5

    def reset(self) -> None:
        self.zoom, self.cx, self.cy = 1.0, 0.5, 0.5

    def clamp(self) -> None:
        self.zoom = min(MAX_ZOOM, max(MIN_ZOOM, self.zoom))
        self.cx = min(1.0, max(0.0, self.cx))
        self.cy = min(1.0, max(0.0, self.cy))

    def zoom_by(self, factor: float, u: float = 0.0, v: float = 0.0) -> None:
        """Zoom, keeping the point at image-fraction offset (u, v) from the centre fixed."""
        old = self.zoom
        self.zoom = min(MAX_ZOOM, max(MIN_ZOOM, old * factor))
        real = self.zoom / old
        self.cx += u * (1 - 1 / real)
        self.cy += v * (1 - 1 / real)
        self.clamp()

    def pan_by(self, dx_frac: float, dy_frac: float) -> None:
        self.cx -= dx_frac
        self.cy -= dy_frac
        self.clamp()


def fit_scale(iw: int, ih: int, bw: int, bh: int) -> float:
    if min(iw, ih, bw, bh) <= 0:
        return 1.0
    return min(bw / iw, bh / ih)


def view_box(iw: int, ih: int, bw: int, bh: int, view: View):
    """What to draw for one pane.

    -> (src_box, dst_rect) or None. src_box = (x0, y0, x1, y1) in image pixels,
    dst_rect = (x, y, w, h) in pane pixels; None when nothing is visible.
    """
    s = fit_scale(iw, ih, bw, bh) * view.zoom
    vw, vh = bw / s, bh / s
    x0, y0 = view.cx * iw - vw / 2, view.cy * ih - vh / 2
    sx0, sy0 = max(0.0, x0), max(0.0, y0)
    sx1, sy1 = min(float(iw), x0 + vw), min(float(ih), y0 + vh)
    if sx1 <= sx0 or sy1 <= sy0:
        return None
    dx0, dy0 = round((sx0 - x0) * s), round((sy0 - y0) * s)
    dx1, dy1 = round((sx1 - x0) * s), round((sy1 - y0) * s)
    return (sx0, sy0, sx1, sy1), (dx0, dy0, max(1, dx1 - dx0), max(1, dy1 - dy0))


def cursor_offset(mx: float, my: float, iw: int, ih: int, bw: int, bh: int,
                  view: View) -> tuple[float, float]:
    """Cursor position relative to the pane centre, in image-fraction units."""
    s = fit_scale(iw, ih, bw, bh) * view.zoom
    return (mx - bw / 2) / (s * iw), (my - bh / 2) / (s * ih)
