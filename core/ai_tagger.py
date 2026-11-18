"""
core/ai_tagger.py
-----------------
Automatic tag suggestions from the picture itself, with a local ONNX model
(no network after the one-time download, no account, nothing leaves your PC).

Models (chosen in the page's ⚙ settings, see core/tagger_settings.py):
    Z3D-E621-ConvNeXt   (toynya/Z3D-E621-Convnext)       e621 vocabulary, matches the booru best
    WD SwinV2 Tagger v3 (SmilingWolf/wd-swinv2-tagger-v3) Danbooru vocabulary, many characters
    Custom              any ONNX tagger you point to (folder, file names, preprocessing)
Each model lives in  <app folder>/models/<key>/  as an .onnx file plus a tag list (CSV with
one tag per output). A ModelSpec describes how to read it.

    installed() / download()     the model files
    AITagger.predict(path)       {tag: score 0..1} for one image
    ScoreStore                   scores cached in hoard.db (an image is analysed once)
    suggest(scores, store, ...)  scores -> suggestions: synonyms renamed to the real tag,
                                 tags the image already has left out, unknown tags flagged

Suggestions are only proposals: the Tagger shows them as ghost chips and nothing is
written until the user accepts them.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from core.tag_store import TagStore, norm_tag

log = logging.getLogger(__name__)

MIN_KEEP = 0.10            # scores below this are not stored
DEFAULT_THRESHOLD = 0.50


class ModelError(Exception):
    """User-facing problem with the model (missing, broken, download failed)."""


@dataclass(frozen=True)
class ModelSpec:
    key: str
    title: str
    base_url: str                          # "" = no download (custom model)
    files: tuple[str, ...]                 # (model file, tag list file)
    size_mb: int
    input_size: int = 448
    name_col: str = "name"                 # CSV column holding the tag
    category_col: str = ""                 # CSV column with the tag category ("" = none)
    skip_categories: tuple[str, ...] = ()  # categories never suggested (e.g. ratings)
    bgr: bool = True                       # channel order the model was trained with
    unit_scale: bool = False               # True = values 0-1, False = 0-255
    vocabulary: str = ""                   # shown in the settings
    note: str = ""
    folder: str = ""                       # custom models: where the files are

    @property
    def model_file(self) -> str:
        return self.files[0]

    @property
    def tags_file(self) -> str:
        return self.files[1]


Z3D = ModelSpec(
    key="z3d-e621",
    title="Z3D-E621-ConvNeXt",
    base_url="https://huggingface.co/toynya/Z3D-E621-Convnext/resolve/main/",
    files=("model.onnx", "tags-selected.csv"),
    size_mb=390,
    vocabulary="e621 tags",
    note="Trained on e621, so tags match your booru. Best default.",
)

WD_V3 = ModelSpec(
    key="wd-swinv2-v3",
    title="WD SwinV2 Tagger v3",
    base_url="https://huggingface.co/SmilingWolf/wd-swinv2-tagger-v3/resolve/main/",
    files=("model.onnx", "selected_tags.csv"),
    size_mb=467,
    category_col="category",
    skip_categories=("9",),                # 9 = rating
    vocabulary="Danbooru tags",
    note="Knows many characters and franchises. Danbooru names differ from e621: "
         "synonyms on the Tags page map them, the rest shows as unknown.",
)

MODELS: dict[str, ModelSpec] = {m.key: m for m in (Z3D, WD_V3)}
DEFAULT_KEY = Z3D.key


def model_dir(spec: ModelSpec = Z3D) -> Path:
    if spec.folder:
        return Path(spec.folder)
    return Path(__file__).resolve().parent.parent / "models" / spec.key


def installed(spec: ModelSpec = Z3D, folder: Path | None = None) -> bool:
    folder = folder or model_dir(spec)
    return all((folder / f).is_file() and (folder / f).stat().st_size > 0 for f in spec.files)


def download(spec: ModelSpec = Z3D, folder: Path | None = None,
             progress: Callable[[int, int, str], None] | None = None,
             cancel: Callable[[], bool] | None = None, session=None) -> None:
    """Download the model files (blocking; run it in a thread).

    progress(done_bytes, total_bytes, file_name). Files are written as *.part and
    renamed when complete, so an interrupted download never leaves a broken model.
    """
    import requests
    http = session or requests
    folder = folder or model_dir(spec)
    folder.mkdir(parents=True, exist_ok=True)
    for name in spec.files:
        target = folder / name
        if target.is_file() and target.stat().st_size > 0:
            continue
        part = folder / (name + ".part")
        try:
            with http.get(spec.base_url + name, stream=True, timeout=30,
                          headers={"User-Agent": "BooruManager/1.0"}) as resp:
                if resp.status_code != 200:
                    raise ModelError(f"Download of {name} failed: HTTP {resp.status_code}.")
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content(1 << 20):
                        if cancel and cancel():
                            raise ModelError("Download cancelled.")
                        fh.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total, name)
            if part.stat().st_size == 0:
                raise ModelError(f"{name} came back empty.")
            part.replace(target)
        except ModelError:
            part.unlink(missing_ok=True)
            raise
        except Exception as e:                       # network trouble, disk full, ...
            part.unlink(missing_ok=True)
            raise ModelError(f"Download of {name} failed: {e}") from e


# ── Running the model ────────────────────────────────────────────────────────

def preprocess(img: Image.Image, size: int = 448, bgr: bool = True,
               unit_scale: bool = False) -> np.ndarray:
    """Picture -> (1, size, size, 3) float32 as the model wants it: centred on a white
    square, bicubic resize, BGR (or RGB) channel order, values 0-255 (or 0-1)."""
    rgba = img.convert("RGBA")
    flat = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    rgb = flat.convert("RGB")
    w, h = rgb.size
    side = max(w, h)
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    canvas.paste(rgb, ((side - w) // 2, (side - h) // 2))
    if side != size:
        canvas = canvas.resize((size, size), Image.BICUBIC)
    arr = np.asarray(canvas, dtype=np.float32)
    if bgr:
        arr = arr[:, :, ::-1]
    if unit_scale:
        arr = arr / 255.0
    return arr[None].copy()


def _providers() -> list[str]:
    import onnxruntime as ort
    have = ort.get_available_providers()
    return [p for p in ("CUDAExecutionProvider", "DmlExecutionProvider") if p in have] + \
           ["CPUExecutionProvider"]


class AITagger:
    def __init__(self, spec: ModelSpec = Z3D, folder: Path | None = None) -> None:
        self.spec = spec
        self._folder = folder or model_dir(spec)
        self._session = None
        self._tags: list[str] = []
        self._skip: set[int] = set()          # outputs that are never suggested (ratings)
        self._lock = threading.Lock()
        self._nchw = False

    @property
    def loaded(self) -> bool:
        return self._session is not None

    def load(self) -> None:
        """Open the model (takes a few seconds; call from a thread)."""
        with self._lock:
            if self._session is not None:
                return
            if not installed(self.spec, self._folder):
                raise ModelError("The tagging model is not downloaded yet.")
            try:
                import onnxruntime as ort
            except ImportError as e:
                raise ModelError("onnxruntime is not installed (pip install onnxruntime).") from e
            try:
                tags: list[str] = []
                skip: set[int] = set()
                with open(self._folder / self.spec.tags_file, encoding="utf-8-sig",
                          newline="") as fh:
                    for r in csv.DictReader(fh):
                        if not r.get(self.spec.name_col):
                            continue
                        if self.spec.category_col and \
                                str(r.get(self.spec.category_col, "")).strip() in self.spec.skip_categories:
                            skip.add(len(tags))
                        tags.append(norm_tag(r[self.spec.name_col]))
                session = ort.InferenceSession(str(self._folder / self.spec.model_file),
                                               providers=_providers())
            except Exception as e:
                raise ModelError(f"Could not open the model: {e}") from e
            out_dim = session.get_outputs()[0].shape[-1]
            if isinstance(out_dim, int) and out_dim != len(tags):
                raise ModelError(f"Model has {out_dim} outputs but the tag list has {len(tags)}.")
            shape = session.get_inputs()[0].shape
            self._nchw = len(shape) == 4 and shape[1] == 3 and shape[-1] != 3
            self._tags, self._skip, self._session = tags, skip, session
            log.info("AI tagger loaded: %d tags, providers %s", len(tags), session.get_providers())

    def predict(self, path: str | Path) -> dict[str, float]:
        """{tag: score} for every tag scoring at least MIN_KEEP, best first."""
        if self._session is None:
            self.load()
        try:
            with Image.open(path) as im:
                x = preprocess(im, self.spec.input_size, self.spec.bgr, self.spec.unit_scale)
        except Exception as e:
            raise ModelError(f"Cannot read {Path(path).name}: {e}") from e
        if self._nchw:
            x = x.transpose(0, 3, 1, 2)
        y = self._session.run(None, {self._session.get_inputs()[0].name: x})[0][0]
        if y.min() < 0.0 or y.max() > 1.0:           # logits instead of probabilities
            y = 1.0 / (1.0 + np.exp(-y))
        order = np.argsort(-y)
        return {self._tags[i]: round(float(y[i]), 3) for i in order
                if y[i] >= MIN_KEEP and i not in self._skip}


# ── Score cache ──────────────────────────────────────────────────────────────

class ScoreStore:
    """Scores per image, kept in hoard.db so an image is analysed only once."""

    def __init__(self, con: sqlite3.Connection, model_key: str = Z3D.key) -> None:
        self._con = con
        self._key = model_key
        self._lock = threading.RLock()
        with self._lock:
            self._con.execute("""
                CREATE TABLE IF NOT EXISTS ai_scores (
                    filepath TEXT NOT NULL,
                    model    TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size     INTEGER NOT NULL,
                    scores   TEXT NOT NULL,
                    PRIMARY KEY (filepath, model))""")
            self._con.commit()

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int]:
        st = path.stat()
        return st.st_mtime_ns, st.st_size

    def get(self, path: str | Path) -> dict[str, float] | None:
        path = Path(path)
        try:
            stamp = self._stamp(path)
        except OSError:
            return None
        with self._lock:
            row = self._con.execute(
                "SELECT mtime_ns, size, scores FROM ai_scores WHERE filepath=? AND model=?",
                (str(path), self._key)).fetchone()
        if row is None or (row[0], row[1]) != stamp:
            return None
        return json.loads(row[2])

    def put(self, path: str | Path, scores: dict[str, float]) -> None:
        path = Path(path)
        mtime, size = self._stamp(path)
        with self._lock, self._con:
            self._con.execute(
                "INSERT OR REPLACE INTO ai_scores (filepath, model, mtime_ns, size, scores) "
                "VALUES (?,?,?,?,?)",
                (str(path), self._key, mtime, size, json.dumps(scores)))

    def has(self, path: str | Path) -> bool:
        return self.get(path) is not None

    def count(self) -> int:
        with self._lock:
            return self._con.execute("SELECT COUNT(*) FROM ai_scores WHERE model=?",
                                     (self._key,)).fetchone()[0]


def scores_for(tagger: AITagger, cache: ScoreStore, path: str | Path) -> dict[str, float]:
    """Cached scores, running the model only when the image was never analysed."""
    got = cache.get(path)
    if got is None:
        got = tagger.predict(path)
        cache.put(path, got)
    return got


def score_many(tagger: AITagger, cache: ScoreStore, paths: list[Path],
               progress: Callable[[int, int], None] | None = None,
               cancel: Callable[[], bool] | None = None) -> tuple[int, list[tuple[str, str]]]:
    """Analyse images that have no scores yet (blocking). -> (analysed, [(path, error)])."""
    todo = [p for p in paths if not cache.has(p)]
    errors: list[tuple[str, str]] = []
    done = 0
    for n, p in enumerate(todo, 1):
        if cancel and cancel():
            break
        try:
            cache.put(p, tagger.predict(p))
            done += 1
        except (ModelError, OSError) as e:
            errors.append((str(p), str(e)))
        if progress:
            progress(n, len(todo))
    return done, errors


# ── Scores -> suggestions ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Suggestion:
    tag: str
    score: float
    known: bool          # on the Tags page (always True when no Tags page is given)


def suggest(scores: dict[str, float], store: TagStore | None, have: list[str] | set[str],
            threshold: float = DEFAULT_THRESHOLD, only_known: bool = False,
            limit: int = 80) -> list[Suggestion]:
    """Tags to propose for an image, best first.

    Synonyms are shown as the real tag, tags the image already has are left out, and
    (with only_known) tags that are not on the Tags page are dropped.
    """
    have_set = {norm_tag(t) for t in have}
    out: list[Suggestion] = []
    seen: set[str] = set()
    for tag, score in sorted(scores.items(), key=lambda kv: -kv[1]):
        if score < threshold or len(out) >= limit:
            break
        found = store.find(tag) if store is not None else None
        name = found["name"] if found else norm_tag(tag)
        known = found is not None or store is None
        if name in have_set or name in seen or (only_known and not known):
            continue
        seen.add(name)
        out.append(Suggestion(name, score, known))
    return out
