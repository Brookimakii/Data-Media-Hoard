"""
core/tagging.py
---------------
Logic behind the Tagger (no Tk): read and change the tags of many images at once.

Where tags live (unchanged, so the Uploader keeps working):
    * the .txt sidecar  base/artist/.tags/site/image.txt   (space separated)
    * hoard.db  artists_media.tags                          (JSON list)
The artist (first folder under the download folder) is implied by the path and is
never stored in the sidecar; `current_tags` returns the tags without it.

    current_tags(db, base, path)            tags of one image
    resolve(tags, store)                    check tags against the Tags page:
                                            synonym -> real tag, implications added, unknown flagged
    audit(tags, store)                      what is wrong with the tags an image already has
    edit_many(db, base, paths, add, remove) batch add / remove, returns [TagEdit] for undo
    fix_many(db, base, paths, store)        synonyms -> real tags, implied tags added
    undo_edits(db, base, edits)             put the previous tags back
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from core.file_db import FileDB
from core.tag_store import TagStore, norm_tag
from core.upload_data import read_tag_file

log = logging.getLogger(__name__)


# ── Reading ──────────────────────────────────────────────────────────────────

def artist_name(base: Path, path: Path) -> str:
    try:
        return path.relative_to(base).parts[0]
    except (ValueError, IndexError):
        return ""


def current_tags(db: FileDB | None, base: Path, path: Path) -> list[str]:
    """The image's tags (without the artist). Database first, sidecar as fallback."""
    tags: list[str] = []
    if db is not None:
        tags = list(db.get_tags(str(path)))
    if not tags:
        tags = read_tag_file(path, base)
    artist = artist_name(base, path)
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        key = norm_tag(t)
        if key and key != norm_tag(artist) and key not in seen:
            seen.add(key)
            out.append(key)
    return out


# ── Checking against the Tags page ───────────────────────────────────────────

@dataclass
class Resolution:
    tags: list[str]                                         # final list, in order
    renamed: dict[str, str] = field(default_factory=dict)   # synonym -> real tag
    implied: list[str] = field(default_factory=list)        # tags added through implications
    unknown: list[str] = field(default_factory=list)        # not on the Tags page

    @property
    def clean(self) -> bool:
        return not (self.renamed or self.implied or self.unknown)


def resolve(tags: list[str], store: TagStore | None, implications: bool = True) -> Resolution:
    """Normalise `tags`: synonyms become the real tag, implied tags are appended,
    tags the Tags page does not know are reported (and kept)."""
    out: list[str] = []
    res = Resolution(out)

    def add(tag: str, implied: bool = False) -> None:
        if tag and tag not in out:
            out.append(tag)
            if implied:
                res.implied.append(tag)

    for raw in tags:
        tag = norm_tag(raw)
        if not tag:
            continue
        found = store.find(tag) if store is not None else None
        if found is not None and found["name"] != tag:
            res.renamed[tag] = found["name"]
            tag = found["name"]
        elif found is None and store is not None and tag not in res.unknown:
            res.unknown.append(tag)
        add(tag)
        if implications and store is not None and found is not None:
            for imp in store.implication_closure(found["name"]):
                add(imp, implied=imp not in [norm_tag(t) for t in tags])
    return res


def audit(tags: list[str], store: TagStore | None) -> list[str]:
    """Problems of an existing tag list, as short messages (empty = fine)."""
    if store is None:
        return []
    res = resolve(tags, store)
    problems = [f"{old} is a synonym of {new}" for old, new in res.renamed.items()]
    problems += [f"{t} is not on the Tags page" for t in res.unknown]
    problems += [f"{t} is implied but missing" for t in res.implied]
    return problems


# ── Writing ──────────────────────────────────────────────────────────────────

@dataclass
class TagEdit:
    path: Path
    before: list[str]
    after: list[str]

    @property
    def changed(self) -> bool:
        return self.before != self.after


def _write(db: FileDB, base: Path, path: Path, tags: list[str]) -> None:
    from core.media_scan import sync_tags
    sync_tags(db, base, path, tags)


def edit_many(db: FileDB, base: Path, paths: list[Path], add: list[str] = (),
              remove: list[str] = (), store: TagStore | None = None,
              implications: bool = True) -> list[TagEdit]:
    """Add and/or remove tags on every image in `paths`.

    Added tags go through `resolve` (synonyms renamed, implications added); tags that
    are removed are removed as typed or as their real name. Files whose tags do not
    change are not touched. Returns one TagEdit per changed file (use with undo_edits).
    """
    adds = resolve(list(add), store, implications).tags if add else []
    drops = {norm_tag(t) for t in remove}
    if store is not None:
        drops |= {found["name"] for t in remove if (found := store.find(t))}
    edits: list[TagEdit] = []
    for path in paths:
        before = current_tags(db, base, path)
        after = [t for t in before if t not in drops]
        after += [t for t in adds if t not in after]
        if after == before:
            continue
        _write(db, base, path, after)
        edits.append(TagEdit(path, before, after))
    return edits


def fix_many(db: FileDB, base: Path, paths: list[Path], store: TagStore,
             implications: bool = True) -> list[TagEdit]:
    """Clean up the whole tag list of each image: synonyms become the real tag and
    implied tags are added. Tags the Tags page does not know are kept as they are."""
    edits: list[TagEdit] = []
    for path in paths:
        before = current_tags(db, base, path)
        after = resolve(before, store, implications).tags
        if after == before:
            continue
        _write(db, base, path, after)
        edits.append(TagEdit(path, before, after))
    return edits


def set_tags(db: FileDB, base: Path, path: Path, tags: list[str]) -> TagEdit | None:
    """Replace one image's tags (the single-image editor). None when nothing changed."""
    before = current_tags(db, base, path)
    after: list[str] = []
    for t in tags:
        key = norm_tag(t)
        if key and key not in after:
            after.append(key)
    if after == before:
        return None
    _write(db, base, path, after)
    return TagEdit(path, before, after)


def undo_edits(db: FileDB, base: Path, edits: list[TagEdit]) -> int:
    """Restore the tags as they were before `edits`. Returns how many files changed."""
    for e in edits:
        _write(db, base, e.path, e.before)
    return len(edits)
