"""
core/dupes_review.py
--------------------
The decisions of the duplicate review, as plain logic (no Tk).

Every file of a group is in one of three states:

    KEEP   upload it
    THROW  do not upload it (it is flagged; the file stays on disk)
    EDIT   a different piece of work (an edit), not a duplicate: it is uploaded and
           split off the group for good

Until "Process" the user's marks are only staged in memory. Unmarked files get a
suggestion: the best copy is kept, the others are thrown.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.dupe_flags import DupeFlags
from core.dupes import Entry, Group, keeper_reason

KEEP, THROW, EDIT = "keep", "throw", "edit"
STATES = (KEEP, THROW, EDIT)


class Review:
    """Staged marks, keyed by file identity (so they survive re-grouping)."""

    def __init__(self) -> None:
        self.marks: dict[str, str] = {}

    def mark(self, key: str, state: str) -> None:
        if state not in STATES:
            raise ValueError(state)
        self.marks[key] = state

    def clear(self, keys) -> None:
        for k in keys:
            self.marks.pop(k, None)

    def touched(self, g: Group, entries: list[Entry]) -> bool:
        return any(entries[i].key in self.marks for i in g.members)

    def states(self, g: Group, entries: list[Entry], flagged: set[str]) -> dict[int, str]:
        """Final state of every member: the user's mark, else the suggestion."""
        marks = {i: self.marks.get(entries[i].key) for i in g.members}
        explicit_keep = any(s == KEEP for s in marks.values())
        suggested = None
        if not explicit_keep:
            suggested = next((i for i in g.members
                              if marks[i] is None and entries[i].key not in flagged), None)
        out = {}
        for i in g.members:
            out[i] = marks[i] or (KEEP if i == suggested else THROW)
        return out


def is_done(g: Group, entries: list[Entry], flagged: set[str]) -> bool:
    """Nothing left to decide: at most one file of the group is still unflagged."""
    return sum(1 for i in g.members if entries[i].key not in flagged) <= 1


@dataclass
class Plan:
    flag: list[tuple[str, str]] = field(default_factory=list)     # (path, reason)
    unflag: list[str] = field(default_factory=list)
    ignore: list[tuple[str, str]] = field(default_factory=list)   # not-duplicate pairs
    groups: int = 0
    kept: int = 0
    thrown: int = 0
    edits: int = 0
    skipped: int = 0               # groups with nothing kept: left untouched


def plan_process(groups: list[Group], entries: list[Entry], review: Review,
                 flagged: set[str]) -> Plan:
    plan = Plan()
    for g in groups:
        st = review.states(g, entries, flagged)
        keepers = [i for i in g.members if st[i] == KEEP] or [i for i in g.members if st[i] == EDIT]
        if not keepers:
            plan.skipped += 1
            continue
        plan.groups += 1
        ref = entries[keepers[0]]
        for i in g.members:
            e = entries[i]
            if st[i] == THROW:
                plan.thrown += 1
                plan.flag.append((e.path, keeper_reason(ref, e)))
            else:
                plan.kept += st[i] == KEEP
                plan.edits += st[i] == EDIT
                if e.key in flagged:
                    plan.unflag.append(e.path)
            if st[i] == EDIT:
                plan.ignore += [(e.path, entries[j].path) for j in g.members if j != i]
    return plan


def apply_plan(plan: Plan, flags: DupeFlags) -> list[tuple]:
    """Write the plan to the database. Returns undo operations for `undo_ops`."""
    undo: list[tuple] = []
    for path, reason in plan.flag:
        was = flags.reason(path) if flags.is_flagged(path) else None
        flags.flag(path, reason)
        undo.append(("flag", path, was))
    for path in plan.unflag:
        if flags.is_flagged(path):
            undo.append(("unflag", path, flags.reason(path)))
            flags.unflag(path)
    already = flags.ignored_pairs()
    for a, b in plan.ignore:
        flags.ignore_pair(a, b)
    new = flags.ignored_pairs() - already
    if new:
        undo.append(("ignore", sorted(new)))
    return undo


def undo_ops(ops: list[tuple], flags: DupeFlags) -> bool:
    """Reverse `apply_plan`. Returns True if pairs were un-ignored (regroup needed)."""
    regroup = False
    for op in reversed(ops):
        if op[0] == "flag":
            flags.unflag(op[1]) if op[2] is None else flags.flag(op[1], op[2])
        elif op[0] == "unflag":
            flags.flag(op[1], op[2])
        elif op[0] == "ignore":
            with flags._db:
                for a, b in op[1]:
                    flags._db.execute("DELETE FROM dupe_ignored WHERE a=? AND b=?", (a, b))
            regroup = True
    return regroup
