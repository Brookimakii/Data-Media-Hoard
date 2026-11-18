"""
core/tagger_settings.py
-----------------------
Which AI model the Tagger uses. Saved next to the app in `tagger_settings.json`.

    load() / save()              the settings file
    active_spec(settings)        the ModelSpec to run (a built-in model, or the custom one)
    list_specs(settings)         every choice shown in the ⚙ settings, custom last
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.ai_tagger import DEFAULT_KEY, MODELS, ModelSpec

log = logging.getLogger(__name__)

CUSTOM_KEY = "custom"


@dataclass
class CustomModel:
    folder: str = ""
    model_file: str = "model.onnx"
    tags_file: str = "tags.csv"
    name_col: str = "name"
    input_size: int = 448
    bgr: bool = True
    unit_scale: bool = False

    @property
    def usable(self) -> bool:
        return bool(self.folder.strip() and self.model_file.strip() and self.tags_file.strip())


@dataclass
class TaggerSettings:
    model: str = DEFAULT_KEY
    custom: CustomModel = field(default_factory=CustomModel)

    def signature(self) -> tuple:
        """Changes whenever the model that should run changes."""
        s = active_spec(self)
        return (s.key, s.folder, s.files, s.input_size, s.bgr, s.unit_scale, s.name_col)


def settings_path() -> Path:
    return Path(__file__).resolve().parent.parent / "tagger_settings.json"


def load(path: Path | None = None) -> TaggerSettings:
    path = path or settings_path()
    s = TaggerSettings()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            c = data.get("custom", {}) or {}
            base = CustomModel()
            s = TaggerSettings(
                model=str(data.get("model", DEFAULT_KEY)),
                custom=CustomModel(
                    folder=str(c.get("folder", "")).strip(),
                    model_file=str(c.get("model_file", base.model_file)).strip() or base.model_file,
                    tags_file=str(c.get("tags_file", base.tags_file)).strip() or base.tags_file,
                    name_col=str(c.get("name_col", base.name_col)).strip() or base.name_col,
                    input_size=max(16, min(2048, int(c.get("input_size", base.input_size)))),
                    bgr=bool(c.get("bgr", base.bgr)),
                    unit_scale=bool(c.get("unit_scale", base.unit_scale))))
    except (OSError, ValueError, TypeError) as e:
        log.warning("Could not read %s (%s), using defaults", path, e)
    return s


def save(s: TaggerSettings, path: Path | None = None) -> None:
    path = path or settings_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(s), indent=2), encoding="utf-8")
    tmp.replace(path)


def custom_spec(c: CustomModel) -> ModelSpec:
    """A ModelSpec for the user's own model. The key depends on the files so scores
    cached for one custom model are never reused for another."""
    tag = re.sub(r"[^a-z0-9]+", "-", f"{Path(c.folder).name}-{c.model_file}".lower()).strip("-")
    return ModelSpec(
        key=f"custom-{tag}", title="Custom model", base_url="",
        files=(c.model_file, c.tags_file), size_mb=0, input_size=c.input_size,
        name_col=c.name_col, bgr=c.bgr, unit_scale=c.unit_scale,
        vocabulary="your tag list", note="Your own ONNX model.", folder=c.folder)


def list_specs(s: TaggerSettings) -> list[ModelSpec]:
    specs = list(MODELS.values())
    if s.custom.usable:
        specs.append(custom_spec(s.custom))
    return specs


def active_spec(s: TaggerSettings) -> ModelSpec:
    if s.model == CUSTOM_KEY and s.custom.usable:
        return custom_spec(s.custom)
    return MODELS.get(s.model, MODELS[DEFAULT_KEY])
