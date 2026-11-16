"""
core/dupes_sort.py
------------------
Ways to order the list of duplicate groups. Pure logic (no Tk).

Each option has a natural direction (what you usually want first: most similar, most
space, A to Z ...); `flip=True` reverses it.
"""

from __future__ import annotations

from typing import Callable

from core.dupes import Entry, Group, artist_of
from core.dupes_review import Review, is_done

# key, label, natural order is descending?, text of the natural / flipped order
SORT_OPTIONS: list[tuple[str, str, bool, tuple[str, str]]] = [
    ("similarity", "Similarity", True, ("most similar first", "least similar first")),
    ("files", "Number of files", True, ("most files first", "fewest files first")),
    ("saving", "Space to free", True, ("most space first", "least space first")),
    ("resolution", "Resolution", True, ("largest first", "smallest first")),
    ("artist", "Artist", False, ("A to Z", "Z to A")),
    ("name", "File name", False, ("A to Z", "Z to A")),
    ("date", "Date", True, ("newest first", "oldest first")),
    ("status", "To review first", False, ("to review first", "done first")),
]
SORT_KEYS = [o[0] for o in SORT_OPTIONS]


def default_descending(key: str) -> bool:
    return next((o[2] for o in SORT_OPTIONS if o[0] == key), True)


def direction_text(key: str, flip: bool) -> str:
    texts = next((o[3] for o in SORT_OPTIONS if o[0] == key), ("", ""))
    return texts[1 if flip else 0]


def sort_groups(groups: list[Group], entries: list[Entry], key: str, flip: bool,
                roots: list[str], review: Review | None = None,
                flagged: set[str] | None = None) -> list[Group]:
    """A new list ordered by `key`, in the option's natural direction (or reversed)."""
    flagged = flagged or set()
    review = review or Review()

    def sim(g: Group) -> float:
        return min(g.scores.values()) if g.scores else 100.0

    def keeper(g: Group) -> Entry:
        return entries[g.members[0]]

    keyfn: dict[str, Callable[[Group], tuple]] = {
        "similarity": lambda g: (sim(g), len(g.members)),
        "files": lambda g: (len(g.members), sim(g)),
        "saving": lambda g: (sum(entries[i].size for i in g.members[1:]), sim(g)),
        "resolution": lambda g: (keeper(g).pixels, keeper(g).size),
        "artist": lambda g: (artist_of(keeper(g).path, roots).lower(), keeper(g).path.lower()),
        "name": lambda g: (keeper(g).path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower(),
                           keeper(g).path.lower()),
        "date": lambda g: (max(entries[i].mtime_ns for i in g.members),),
        "status": lambda g: (2 if is_done(g, entries, flagged)
                             else 1 if review.touched(g, entries) else 0, -sim(g)),
    }
    if key not in keyfn:
        return list(groups)
    return sorted(groups, key=keyfn[key], reverse=default_descending(key) != flip)
