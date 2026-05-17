"""
core/catalogue.py
-----------------
Domain-level loaders for the two main data files:
  - artists.yaml   → list of Artist dicts
  - config.yaml    → application configuration

Both functions return plain Python objects (dicts / lists).
Error handling raises typed exceptions; callers (UI or CLI) decide
how to surface them to the user.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.yaml_io import read_yaml, YAMLError


# ── Typed exceptions ───────────────────────────────────────────────────────────

class CatalogueError(Exception):
    """Raised when the artists catalogue cannot be loaded or is malformed."""


class ConfigError(Exception):
    """Raised when the application config cannot be loaded or is malformed."""


# ── Artist catalogue ───────────────────────────────────────────────────────────

#: Expected structure of a single artist entry (for documentation purposes).
#:
#: {
#:   "name":  str,
#:   "tags":  list[str],          # optional
#:   "notes": str,                # optional
#:   "media": {
#:     "<category>": {            # e.g. aggregators, socials, paid
#:       "<site>": str | list[str]
#:     }
#:   }
#: }

def load_artists(path: str | Path) -> list[dict[str, Any]]:
    """
    Load and validate the artists catalogue YAML file.

    Returns a list of artist dicts.
    Raises CatalogueError on missing file, parse failure, or wrong structure.
    """
    path = Path(path)
    try:
        data = read_yaml(path)
    except YAMLError as e:
        raise CatalogueError(str(e)) from e

    if data is None:
        return []

    if not isinstance(data, dict):
        raise CatalogueError(
            f"{path.name}: expected a YAML mapping at the top level, got {type(data).__name__}"
        )

    artists = data.get("artists", [])

    if not isinstance(artists, list):
        raise CatalogueError(
            f"{path.name}: 'artists' key must be a list, got {type(artists).__name__}"
        )

    # Light validation — warn but don't reject entries with missing fields
    validated = []
    for i, entry in enumerate(artists):
        if not isinstance(entry, dict):
            raise CatalogueError(
                f"{path.name}: entry #{i} is not a mapping (got {type(entry).__name__})"
            )
        if "name" not in entry:
            raise CatalogueError(
                f"{path.name}: entry #{i} is missing the required 'name' field"
            )
        validated.append(entry)

    return validated


def get_all_sites(artists: list[dict]) -> set[str]:
    """
    Return the set of all site names referenced across all artists.
    Useful for cross-checking against downloadable_sites in the config.
    """
    sites: set[str] = set()
    for artist in artists:
        for _cat, entries in (artist.get("media") or {}).items():
            if isinstance(entries, dict):
                sites.update(entries.keys())
    return sites


# ── Application config ─────────────────────────────────────────────────────────

#: Default configuration values used when a key is absent from config.yaml.
_CONFIG_DEFAULTS: dict[str, Any] = {
    "downloadable_sites": [],
    "log_dir":            "./logs",
    "download_dir":       "./download",
    "gdl_config":         "./config.json",
    "archives_dir":       "./archives",
    "ui": {
        "fonts": {
            # Main UI family for labels/buttons.
            "ui_family": "Segoe UI",
            # Monospace family for table-like content.
            "mono_family": "Consolas",
            # Emoji-capable family (Windows color emoji).
            "emoji_family": "Segoe UI Emoji",
            # Optional serif display family.
            "serif_family": "Georgia",
        }
    },
}


def load_config(path: str | Path) -> dict[str, Any]:
    """
    Load the application config YAML file.

    Returns a dict with all keys, falling back to _CONFIG_DEFAULTS for
    any key that is absent from the file.
    Raises ConfigError on missing file, parse failure, or wrong structure.
    """
    path = Path(path)
    try:
        data = read_yaml(path)
    except YAMLError as e:
        raise ConfigError(str(e)) from e

    if data is None:
        data = {}

    if not isinstance(data, dict):
        raise ConfigError(
            f"{path.name}: expected a YAML mapping at the top level, got {type(data).__name__}"
        )

    # Merge with defaults so callers can always rely on every key existing.
    # Keep nested UI/font defaults stable even when config.yaml only provides
    # a partial override.
    config = {**_CONFIG_DEFAULTS, **data}
    ui_defaults = (_CONFIG_DEFAULTS.get("ui") or {})
    ui_data = data.get("ui") if isinstance(data.get("ui"), dict) else {}
    config["ui"] = {**ui_defaults, **ui_data}

    font_defaults = (ui_defaults.get("fonts") or {}) if isinstance(ui_defaults, dict) else {}
    ui_fonts = ui_data.get("fonts") if isinstance(ui_data.get("fonts"), dict) else {}
    if isinstance(config["ui"], dict):
        config["ui"]["fonts"] = {**font_defaults, **ui_fonts}

    # Normalise downloadable_sites to a plain set for O(1) membership checks
    raw_sites = config.get("downloadable_sites", [])
    if not isinstance(raw_sites, list):
        raise ConfigError(
            f"{path.name}: 'downloadable_sites' must be a list, got {type(raw_sites).__name__}"
        )
    config["downloadable_sites"] = set(raw_sites)

    return config


def get_downloadable_sites(config: dict[str, Any]) -> set[str]:
    """Convenience accessor — returns the set of downloadable site names."""
    return config.get("downloadable_sites", set())