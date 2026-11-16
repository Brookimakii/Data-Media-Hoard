"""
core/videos.py
--------------
Video and animation access for the duplicate finder (no Tk, no database).

    available()                  ffmpeg and ffprobe are on PATH
    probe(path)                  duration, size, frame rate, bitrate, codecs
    sample_frames(path, info)    8 grey 32x32 frames spread over the clip + a poster JPEG
    animation_frames(path)       the same for animated GIF / WebP / APNG, using Pillow only
    frame_at(path, seconds)      one full-size frame (for the comparison window)

Frames are taken at the same fractions of the clip for every file (10% ... 90%), so two
encodes of the same video produce the same pictures whatever their resolution or codec.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageSequence

log = logging.getLogger(__name__)

N_FRAMES = 8
FRACTIONS = tuple((i + 1) / (N_FRAMES + 1) for i in range(N_FRAMES))     # 0.11 ... 0.89
POSTER_FRACTION = FRACTIONS[1]
PREVIEW_BOX = (96, 72)
_THUMB = 32
_TIMEOUT = 60
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class VideoError(Exception):
    pass


@dataclass
class VideoInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bitrate: int = 0            # bits per second, whole file
    vcodec: str = ""
    acodec: str = ""            # "" = no audio track


@dataclass
class Sampled:
    frames: np.ndarray          # (N, 32, 32) uint8 grey
    poster: bytes               # small JPEG
    info: VideoInfo


def available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(cmd: list[str], timeout: int = _TIMEOUT) -> bytes:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        raise VideoError(f"{cmd[0]} timed out") from e
    except OSError as e:
        raise VideoError(f"cannot run {cmd[0]}: {e}") from e
    if r.returncode != 0:
        lines = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise VideoError(lines[-1] if lines else f"{cmd[0]} failed")
    return r.stdout


def _ratio(text: str) -> float:
    try:
        a, _, b = text.partition("/")
        return float(a) / float(b) if b else float(a)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: str | Path) -> VideoInfo:
    out = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                "-show_streams", str(path)])
    data = json.loads(out or b"{}")
    info = VideoInfo()
    fmt = data.get("format", {})
    info.duration = float(fmt.get("duration") or 0)
    info.bitrate = int(float(fmt.get("bit_rate") or 0))
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info.vcodec:
            if s.get("disposition", {}).get("attached_pic"):      # cover art, not a video
                continue
            info.vcodec = s.get("codec_name", "")
            info.width, info.height = int(s.get("width") or 0), int(s.get("height") or 0)
            info.fps = _ratio(s.get("avg_frame_rate") or s.get("r_frame_rate") or "0")
            info.duration = info.duration or float(s.get("duration") or 0)
            rot = int((s.get("tags") or {}).get("rotate", 0) or 0)
            for sd in s.get("side_data_list", []) or []:
                rot = int(sd.get("rotation", rot) or rot)
            if abs(rot) % 180 == 90:                              # phone video stored sideways
                info.width, info.height = info.height, info.width
        elif s.get("codec_type") == "audio" and not info.acodec:
            info.acodec = s.get("codec_name", "")
    return info


def _grab(path: str, seconds: float, vf: str, extra: list[str]) -> bytes:
    return _run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0.0, seconds):.3f}", "-i",
                 path, "-frames:v", "1", "-an", "-sn", "-vf", vf, *extra, "-"])


def _grey_frame(path: str, seconds: float) -> np.ndarray:
    raw = _grab(path, seconds, f"scale={_THUMB}:{_THUMB}:flags=area,format=gray",
                ["-f", "rawvideo"])
    if len(raw) != _THUMB * _THUMB:
        raise VideoError("no frame at that position")
    return np.frombuffer(raw, dtype=np.uint8).reshape(_THUMB, _THUMB).copy()


def _poster(path: str, seconds: float) -> bytes:
    vf = f"scale={PREVIEW_BOX[0]}:{PREVIEW_BOX[1]}:force_original_aspect_ratio=decrease"
    return _grab(path, seconds, vf, ["-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "6"])


def sample_frames(path: str | Path, info: VideoInfo | None = None) -> Sampled:
    """8 frames at 11%..89% of the clip (read in parallel) and a poster picture."""
    path = str(path)
    info = info or probe(path)
    if info.duration <= 0 or not info.vcodec:
        raise VideoError("no video stream or unknown duration")
    times = [info.duration * f for f in FRACTIONS]
    # stay inside the clip: seeking to the very end can return nothing
    times = [min(t, max(0.0, info.duration - 0.2)) for t in times]
    with ThreadPoolExecutor(max_workers=4) as pool:
        grey = list(pool.map(lambda t: _grey_frame(path, t), times))
        poster = pool.submit(_poster, path, times[1]).result()
    return Sampled(np.stack(grey), poster, info)


def frame_at(path: str | Path, seconds: float, max_width: int = 1920) -> Image.Image:
    """One frame at full resolution (capped in width) for the comparison window."""
    raw = _grab(str(path), seconds, f"scale='min({max_width},iw)':-2",
                ["-f", "image2pipe", "-c:v", "png"])
    with Image.open(io.BytesIO(raw)) as im:
        return im.convert("RGB")


# ── Animated images (GIF / WebP / APNG): Pillow only ─────────────────────────

def is_animated(path: str | Path) -> bool:
    try:
        with Image.open(path) as im:
            return getattr(im, "n_frames", 1) > 1
    except Exception:
        return False


def animation_frames(path: str | Path) -> Sampled:
    """Same output as sample_frames for an animated image, without ffmpeg."""
    with Image.open(path) as im:
        n = int(getattr(im, "n_frames", 1))
        if n < 2:
            raise VideoError("not animated")
        durations = []
        for i in range(n):
            im.seek(i)
            durations.append(max(10, int(im.info.get("duration", 100) or 100)))
        total = sum(durations) / 1000.0
        pick = [min(n - 1, int(round(f * (n - 1)))) for f in FRACTIONS]
        grey, poster = [], b""
        for k, i in enumerate(pick):
            im.seek(i)
            rgba = im.convert("RGBA")
            flat = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
            grey.append(np.asarray(flat.convert("L").resize((_THUMB, _THUMB), Image.LANCZOS),
                                   dtype=np.uint8).copy())
            if k == 1:
                small = flat.convert("RGB")
                small.thumbnail(PREVIEW_BOX)
                buf = io.BytesIO()
                small.save(buf, "JPEG", quality=78)
                poster = buf.getvalue()
        w, h = im.size
        info = VideoInfo(duration=total, width=w, height=h, fps=n / total if total else 0.0,
                         bitrate=0, vcodec=(im.format or "").lower())
    return Sampled(np.stack(grey), poster, info)


def animation_frame_at(path: str | Path, seconds: float) -> Image.Image:
    with Image.open(path) as im:
        t = 0.0
        for frame in ImageSequence.Iterator(im):
            t += max(10, int(frame.info.get("duration", 100) or 100)) / 1000.0
            if t >= seconds:
                break
        rgba = im.convert("RGBA")
        return Image.alpha_composite(Image.new("RGBA", rgba.size, (30, 30, 36, 255)), rgba).convert("RGB")
