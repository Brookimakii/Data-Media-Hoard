"""
core/booru_settings.py
----------------------
Where to push, and as whom. Saved next to the app in `booru_settings.json`
(listed in .gitignore: it holds the API key).

Environment variables override the file, e.g. from the .env file:
    BOORU_SERVER, BOORU_USER, BOORU_API_KEY
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_SERVER = "http://localhost:3000"


@dataclass
class BooruSettings:
    server: str = DEFAULT_SERVER
    username: str = ""
    api_key: str = ""
    auto_push: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.server.strip() and self.username.strip() and self.api_key.strip())


def settings_path() -> Path:
    return Path(__file__).resolve().parent.parent / "booru_settings.json"


def load_settings(path: Path | None = None) -> BooruSettings:
    path = path or settings_path()
    s = BooruSettings()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            s = BooruSettings(
                server=str(data.get("server", s.server)).strip() or DEFAULT_SERVER,
                username=str(data.get("username", "")).strip(),
                api_key=str(data.get("api_key", "")).strip(),
                auto_push=bool(data.get("auto_push", True)),
            )
    except (OSError, ValueError) as e:
        log.warning("Could not read %s (%s), using defaults", path, e)

    s.server = os.environ.get("BOORU_SERVER", s.server).strip() or DEFAULT_SERVER
    s.username = os.environ.get("BOORU_USER", s.username).strip()
    s.api_key = os.environ.get("BOORU_API_KEY", s.api_key).strip()
    return s


def save_settings(s: BooruSettings, path: Path | None = None) -> None:
    path = path or settings_path()
    data = asdict(s)
    data["server"] = data["server"].strip().rstrip("/") or DEFAULT_SERVER
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)            # best effort: the file holds the API key
    except OSError:
        pass
    tmp.replace(path)
