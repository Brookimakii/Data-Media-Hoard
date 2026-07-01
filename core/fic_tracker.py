"""
core/fic_tracker.py
-------------------
Parser + writer for the AO3 fic tracker file format.

File format:
    # Fandom Name                                              ← fandom header (comment)
    https://ao3.org/works/123  # 🟢 2024-01-01 - Title        ← enabled fic (no leading #)
    # https://ao3.org/works/456  # 🔴 2023-11-01 - Title      ← disabled fic (leading #)

A fandom header is a comment line whose content is NOT a URL and contains
no status emoji.

Status legend:  🟢 Finished  🟡 Dropped  🔴 Ongoing
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:
    import yaml as _yaml
    _YAML_OK = True
except ImportError:
    _YAML_OK = False

STATUS_EMOJI = {"🟢", "🟡", "🔴", "🟠"}

STATUS_LABEL = {
    "🟢": "Finished",
    "🟡": "Dropped",
    "🔴": "Ongoing",
    "🟠": "Stale",    # ongoing, no update for N months
}

# Matches a fic line — with or without a leading "#" (disabled marker).
# Group 1: leading "#" if disabled, else ""
# Group 2: URL
# Group 3: status emoji
# Group 4: date yyyy-mm-dd
# Group 5: rest (title + optional extras)
_FIC_RE = re.compile(
    r"^(#?)\s*"                    # group 1: optional leading # = disabled
    r"(https?://\S+)"              # group 2: URL (must start with http)
    r"\s+#\s*"                     # separator " # "
    r"([🟢🟡🔴🟠])"               # group 3: status emoji
    r"\s+"
    r"(\d{4}-\d{2}-\d{2})"        # group 4: date
    r"\s*-\s*"
    r"(.+)$",                      # group 5: rest
    re.UNICODE,
)

# Fandom header: a comment line that is not a fic line
_FANDOM_RE = re.compile(
    r"^#\s*(.+)$",
    re.UNICODE,
)

_EXTRA_RE = re.compile(r"\[([^\]]+)\]", re.UNICODE)


@dataclass
class Fic:
    fandom:     str
    url:        str
    status:     str           # emoji
    date:       str           # yyyy-mm-dd
    title:      str
    word_count: str  = ""
    chapters:   str  = ""
    enabled:    bool = True   # False → line has a leading #
    is_series:  bool = False  # True → URL is a series, not a single work
    line_index: int  = -1     # 0-based index into raw_lines
    # Rich metadata (from YAML sidecar, not stored in .txt)
    summary:       str  = ""
    categories:    list = field(default_factory=list)
    tags:          list = field(default_factory=list)
    authors:       list = field(default_factory=list)
    rating:        str  = ""
    fandoms_list:  list = field(default_factory=list)
    relationships: list = field(default_factory=list)
    characters:    list = field(default_factory=list)
    warnings:      list = field(default_factory=list)


@dataclass
class FicFile:
    fandoms:   dict[str, list[Fic]] = field(default_factory=dict)
    raw_lines: list[str]            = field(default_factory=list)
    path:      Path | None          = None

    @property
    def all_fics(self) -> list[Fic]:
        return [f for fics in self.fandoms.values() for f in fics]


def _sync_fic_to_db(fic_file: FicFile, fic: Fic) -> None:
    """Write/refresh a fic row in FileDB.fanfiction if DB is available."""
    try:
        from core.file_db import get_db
        db = get_db()
        if not db:
            return
        db.upsert_fanfiction(
            url=fic.url,
            title=fic.title,
            fandom=fic.fandom,
            status=fic.status,
            updated_at=fic.date,
            source_file=str(fic_file.path) if fic_file.path else "",
            enabled=fic.enabled,
            is_series=fic.is_series,
            word_count=fic.word_count,
            chapters=fic.chapters,
            summary=fic.summary,
            categories=fic.categories,
            tags=fic.tags,
            authors=fic.authors,
            rating=fic.rating,
            fandoms_list=fic.fandoms_list,
            relationships=fic.relationships,
            characters=fic.characters,
            warnings=fic.warnings,
        )
    except Exception as e:
        log.debug("_sync_fic_to_db: failed for %s: %s", fic.url, e)


def _parse_extras(rest: str) -> tuple[str, str, str]:
    extras = _EXTRA_RE.findall(rest)
    title  = _EXTRA_RE.sub("", rest).strip()
    word_count = chapters = ""
    for extra in extras:
        e = extra.strip()
        if "/" in e:
            # work chapter format: "12/20" or "12/?"
            chapters = e
        elif re.match(r"^\d+[kKwW]?$", e):
            word_count = e
        elif re.match(r"^\d+\s+works?", e, re.IGNORECASE):
            # series format: "3 works (45 ch.)" or "3 works"
            chapters = e
        else:
            title += f" [{e}]"
    return title, word_count, chapters


def parse_fic_file(path: str | Path) -> FicFile:
    path   = Path(path)
    result = FicFile(path=path)
    log.debug("parse_fic_file: reading %s", path)

    with open(path, encoding="utf-8") as f:
        raw_lines = f.readlines()
    result.raw_lines = raw_lines

    current_fandom: str = "Unknown"

    for idx, raw_line in enumerate(raw_lines):
        line = raw_line.strip()
        if not line:
            continue

        # Try fic line first (enabled or disabled)
        m = _FIC_RE.match(line)
        if m:
            disabled_marker, url, status, date, rest = (
                m.group(1), m.group(2), m.group(3), m.group(4), m.group(5).strip()
            )
            title, word_count, chapters = _parse_extras(rest)
            fic = Fic(
                fandom=current_fandom,
                url=url,
                status=status,
                date=date,
                title=title,
                word_count=word_count,
                chapters=chapters,
                enabled=disabled_marker == "",   # no leading # → enabled
                is_series="/series/" in url,
                line_index=idx,
            )
            result.fandoms.setdefault(current_fandom, []).append(fic)
            continue

        # Fandom header: comment line with no URL / no status emoji
        m2 = _FANDOM_RE.match(line)
        if m2:
            text = m2.group(1).strip()
            if text and not any(e in text for e in STATUS_EMOJI):
                current_fandom = text

    load_yaml_meta(result)
    for fic in result.all_fics:
        _sync_fic_to_db(result, fic)
    log.debug(
        "parse_fic_file: %s -> %d fandom(s), %d fic(s) total",
        path.name, len(result.fandoms), len(result.all_fics),
    )
    return result


def toggle_fic_enabled(fic_file: FicFile, fic: Fic) -> None:
    """
    Toggle a fic's enabled state, edit the raw line, and save the file.

    Enabled  → line has no leading #  → toggling adds "# " at the start
    Disabled → line has leading "# "  → toggling removes "# " from the start
    """
    if fic.line_index < 0 or fic_file.path is None:
        return

    raw = fic_file.raw_lines[fic.line_index]

    if fic.enabled:
        # Disable: prepend "# "
        new_raw = "# " + raw
        fic.enabled = False
    else:
        # Enable: strip the leading "# " or "#"
        stripped = raw.lstrip()
        if stripped.startswith("# "):
            new_raw = stripped[2:]
        elif stripped.startswith("#"):
            new_raw = stripped[1:]
        else:
            new_raw = raw
        # Preserve original indentation (there likely is none, but be safe)
        indent = len(raw) - len(raw.lstrip())
        new_raw = raw[:indent] + new_raw
        fic.enabled = True

    fic_file.raw_lines[fic.line_index] = new_raw
    fic_file.path.write_text("".join(fic_file.raw_lines), encoding="utf-8")
    log.debug("toggle_fic_enabled: %s -> enabled=%s (%s)", fic.url, fic.enabled, fic_file.path.name)
    _sync_fic_to_db(fic_file, fic)


def update_fic(fic_file: FicFile, fic: Fic,
               date: str, status: str,
               chapters: str, word_count: str) -> None:
    """
    Update a fic's metadata in the raw line and save the file.

    The line format is:
        [# ]<url>  # <emoji> <date> - <title> [extras...]

    We replace everything from the metadata comment (second #) onward.
    The enabled/disabled prefix and URL are preserved exactly.
    """
    if fic.line_index < 0 or fic_file.path is None:
        return

    raw = fic_file.raw_lines[fic.line_index]
    eol = "\r\n" if raw.endswith("\r\n") else "\n"
    line = raw.rstrip("\r\n")

    # Build new metadata comment
    extras = ""
    if word_count:
        extras += f" [{word_count}]"
    if chapters:
        extras += f" [{chapters}]"
    new_comment = f"# {status} {date} - {fic.title}{extras}"

    # Find the metadata comment — the "# <emoji>" part after the URL.
    # We look for "  # <emoji>" to avoid matching the disable prefix "#".
    m = re.search(
        r"\s+#\s*[🟢🟡🔴🟠]\s+\d{4}-\d{2}-\d{2}\s*-\s*.+$",
        line, re.UNICODE,
    )
    if m:
        # Keep everything before the metadata comment, replace from there
        prefix   = line[:m.start()]
        new_line = prefix + "  " + new_comment + eol
    else:
        # Fallback: append the comment if no existing one found
        new_line = line + "  " + new_comment + eol

    fic_file.raw_lines[fic.line_index] = new_line
    fic.date       = date
    fic.status     = status
    fic.chapters   = chapters
    fic.word_count = word_count
    fic_file.path.write_text("".join(fic_file.raw_lines), encoding="utf-8")
    log.debug("update_fic: %s -> status=%s date=%s chapters=%s (%s)",
              fic.url, status, date, chapters, fic_file.path.name)
    _sync_fic_to_db(fic_file, fic)


def is_stale(fic: Fic, stale_months: int) -> bool:
    """
    Return True if the fic is non-finished and hasn't been updated
    in stale_months calendar months.
    """
    if fic.status in ("🟢", "🟡"):
        return False
    if not fic.date:
        return False
    from datetime import date as _date
    try:
        updated = _date.fromisoformat(fic.date)
    except ValueError:
        return False
    today = _date.today()
    months_ago = (today.year - updated.year) * 12 + (today.month - updated.month)
    return months_ago >= stale_months


def add_fic(fic_file: FicFile, fandom: str, url: str,
            status: str, date: str, title: str,
            word_count: str = "", chapters: str = "",
            enabled: bool = True) -> Fic:
    """
    Append a new fic line to the file under the given fandom.
    Creates the fandom header if it doesn't exist yet.
    Returns the newly created Fic.
    """
    if fic_file.path is None:
        raise ValueError("FicFile has no path")

    log.debug("add_fic: adding %s to fandom %r (enabled=%s) in %s",
              url, fandom, enabled, fic_file.path.name)

    # Build the line
    extras = ""
    if word_count:
        extras += f" [{word_count}]"
    if chapters:
        extras += f" [{chapters}]"
    comment = f"# {status} {date} - {title}{extras}"
    prefix  = "" if enabled else "# "
    new_line = f"{prefix}{url}  {comment}\n"

    # Check if the fandom header already exists
    fandom_exists = fandom in fic_file.fandoms

    if fandom_exists:
        # Find the last line of this fandom's block and insert after it
        fics = fic_file.fandoms[fandom]
        if fics:
            insert_after = fics[-1].line_index
        else:
            # Find the header line index
            insert_after = _find_fandom_header_line(fic_file, fandom)
        insert_idx = insert_after + 1
    else:
        # Append fandom header + fic at end of file
        log.debug("add_fic: fandom %r doesn't exist yet, creating header", fandom)
        header_line = f"\n# {fandom}\n"
        fic_file.raw_lines.append(header_line)
        fic_file.raw_lines.append(new_line)
        fic_file.path.write_text("".join(fic_file.raw_lines), encoding="utf-8")
        line_idx = len(fic_file.raw_lines) - 1
        fic = Fic(fandom=fandom, url=url, status=status, date=date,
                  title=title, word_count=word_count, chapters=chapters,
                  enabled=enabled, is_series="/series/" in url,
                  line_index=line_idx)
        fic_file.fandoms.setdefault(fandom, []).append(fic)
        _sync_fic_to_db(fic_file, fic)
        return fic

    fic_file.raw_lines.insert(insert_idx, new_line)
    fic_file.path.write_text("".join(fic_file.raw_lines), encoding="utf-8")

    # Update line indices of all fics after the insertion point
    for fics_list in fic_file.fandoms.values():
        for f in fics_list:
            if f.line_index >= insert_idx:
                f.line_index += 1

    line_idx = insert_idx
    fic = Fic(fandom=fandom, url=url, status=status, date=date,
              title=title, word_count=word_count, chapters=chapters,
              enabled=enabled, is_series="/series/" in url,
              line_index=line_idx)
    fic_file.fandoms.setdefault(fandom, []).append(fic)
    _sync_fic_to_db(fic_file, fic)
    return fic


def _find_fandom_header_line(fic_file: FicFile, fandom: str) -> int:
    """Return the line index of the fandom header, or last line if not found."""
    for idx, line in enumerate(fic_file.raw_lines):
        stripped = line.strip()
        if stripped.startswith("#") and not any(e in stripped for e in STATUS_EMOJI):
            text = stripped.lstrip("#").strip()
            if text == fandom:
                return idx
    return len(fic_file.raw_lines) - 1


# ── YAML sidecar ─────────────────────────────────────────────────────────────
# fics.yaml lives next to fics.txt and stores rich metadata keyed by URL.
# The .txt file remains the source of truth for the list.

def _yaml_path(fic_path: Path) -> Path:
    # Always store YAML next to main.py (parent of the booru_manager package)
    main_dir = Path(__file__).parent.parent
    return main_dir / "fics.yaml"


def load_yaml_meta(fic_file: FicFile) -> None:
    """Load rich metadata from the YAML sidecar into the FicFile's Fic objects."""
    if not fic_file.path or not _YAML_OK:
        return
    ypath = _yaml_path(fic_file.path)
    if not ypath.exists():
        log.debug("load_yaml_meta: no sidecar at %s", ypath)
        return
    try:
        with open(ypath, encoding="utf-8") as f:
            data: dict[str, Any] = _yaml.safe_load(f) or {}
    except Exception as e:
        log.warning("load_yaml_meta: failed to parse %s: %s", ypath, e)
        return

    url_map = {f.url: f for f in fic_file.all_fics}
    matched = 0
    for url, meta in data.items():
        if not isinstance(meta, dict):
            continue
        fic = url_map.get(url)
        if fic is None:
            continue
        matched += 1
        fic.summary       = meta.get("summary", "")
        fic.categories    = meta.get("categories", []) or []
        fic.tags          = meta.get("tags", []) or []
        fic.authors       = meta.get("authors", []) or []
        fic.rating        = meta.get("rating", "")
        fic.fandoms_list  = meta.get("fandoms_list", []) or []
        fic.relationships = meta.get("relationships", []) or []
        fic.characters    = meta.get("characters", []) or []
        fic.warnings      = meta.get("warnings", []) or []
    log.debug("load_yaml_meta: %s -> matched %d/%d fic(s)", ypath, matched, len(data))


def save_yaml_meta(fic_file: FicFile) -> None:
    """Save rich metadata from all Fic objects to the YAML sidecar."""
    if not fic_file.path or not _YAML_OK:
        return
    ypath = _yaml_path(fic_file.path)

    # Load existing data to merge (don't overwrite entries not in memory)
    existing: dict[str, Any] = {}
    if ypath.exists():
        try:
            with open(ypath, encoding="utf-8") as f:
                existing = _yaml.safe_load(f) or {}
        except Exception as e:
            log.warning("save_yaml_meta: failed to read existing %s: %s", ypath, e)

    written = 0
    for fic in fic_file.all_fics:
        if not (fic.summary or fic.categories or fic.tags or fic.authors):
            continue
        existing[fic.url] = {
            "title":         fic.title,
            "summary":       fic.summary,
            "categories":    fic.categories,
            "tags":          fic.tags,
            "authors":       fic.authors,
            "rating":        fic.rating,
            "fandoms_list":  fic.fandoms_list,
            "relationships": fic.relationships,
            "characters":    fic.characters,
            "warnings":      fic.warnings,
        }
        written += 1

    try:
        with open(ypath, "w", encoding="utf-8") as f:
            _yaml.dump(existing, f, allow_unicode=True,
                       default_flow_style=False, sort_keys=False)
        log.debug("save_yaml_meta: wrote %d fic(s) (%d total) to %s", written, len(existing), ypath)
    except Exception as e:
        log.warning("save_yaml_meta: failed to write %s: %s", ypath, e)


def update_fic_meta(fic_file: FicFile, fic: Fic,
                    summary: str = "",
                    categories: list | None = None,
                    tags: list | None = None,
                    authors: list | None = None,
                    rating: str = "",
                    fandoms_list: list | None = None,
                    relationships: list | None = None,
                    characters: list | None = None,
                    warnings: list | None = None) -> None:
    """Update a fic's rich metadata in memory and save to YAML."""
    if summary:                fic.summary       = summary
    if categories  is not None: fic.categories   = categories
    if tags        is not None: fic.tags         = tags
    if authors     is not None: fic.authors      = authors
    if rating:                  fic.rating       = rating
    if fandoms_list is not None: fic.fandoms_list = fandoms_list
    if relationships is not None: fic.relationships = relationships
    if characters  is not None: fic.characters   = characters
    if warnings    is not None: fic.warnings     = warnings
    save_yaml_meta(fic_file)
    _sync_fic_to_db(fic_file, fic)