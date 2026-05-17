"""
utils/yaml_io.py
----------------
Generic YAML read/write helpers.
No domain knowledge — just safe, consistent file I/O for YAML files.
"""

import yaml
from pathlib import Path
from typing import Any


class YAMLError(Exception):
    """Raised when a YAML file cannot be read or written."""


def read_yaml(path: str | Path) -> Any:
    """
    Read and parse a YAML file.

    Returns the parsed object (usually a dict or list).
    Raises YAMLError on missing file or parse failure.
    """
    path = Path(path)
    if not path.exists():
        raise YAMLError(f"File not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise YAMLError(f"Failed to parse {path}:\n{e}") from e
    except OSError as e:
        raise YAMLError(f"Failed to read {path}:\n{e}") from e


def write_yaml(path: str | Path, data: Any, *, create_parents: bool = True) -> None:
    """
    Serialise *data* to a YAML file at *path*.

    Parameters
    ----------
    path            : destination file path
    data            : any YAML-serialisable Python object
    create_parents  : if True, create missing parent directories automatically
    """
    path = Path(path)
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False)
    except OSError as e:
        raise YAMLError(f"Failed to write {path}:\n{e}") from e


def read_yaml_key(path: str | Path, key: str, default: Any = None) -> Any:
    """
    Convenience: read a single top-level key from a YAML file.
    Returns *default* if the file is missing or the key does not exist.
    """
    try:
        data = read_yaml(path)
    except YAMLError:
        return default
    if not isinstance(data, dict):
        return default
    return data.get(key, default)