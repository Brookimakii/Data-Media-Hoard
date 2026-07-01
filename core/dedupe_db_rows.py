"""
dedupe_db_rows.py
------------------
One-time cleanup for hoard.db: merges artists_media rows that point at
the SAME PHYSICAL FILE but were registered under different path spellings
(e.g. ".\\download\\X\\..." vs "download\\X\\..." vs "F:\\Data Media
Hoard\\download\\X\\..."). This happened because older scans stored
whatever path string was typed in, with no normalization — fixed going
forward in core/media_scan.py (it now stores fully resolved paths), but
existing rows from before that fix need a one-time merge.

IMPORTANT — run this from your project root (the folder containing
main.py / config.yaml / hoard.db), the same place you normally run the
app from. Relative paths in the database (like ".\\download\\...") can
only be resolved to the correct real file if the working directory
matches what it was when those rows were first created — which for this
app is always the project root.

Matching strategy (two passes, since path resolution alone can be
unreliable for relative paths if your working directory has ever
changed):
  1. Group rows by resolved absolute path (handles the relative vs.
     absolute spelling differences directly).
  2. Within what's left, ALSO group by (filename, file_size) — two rows
     with the exact same filename and byte size are almost certainly the
     same file under yet another path spelling, even if step 1 didn't
     catch it. This is a heuristic, not a hash comparison, so it prints
     what it's about to merge before doing anything.

For each group, keeps ONE row (preferring the one with useful data
already filled in: non-pending status, tags, hashes — in that order)
and deletes the rest, carrying over any tags/hashes the deleted rows
had that the kept row was missing.

Usage:
    python dedupe_db_rows.py                  # uses ./hoard.db
    python dedupe_db_rows.py path\\to\\hoard.db
    python dedupe_db_rows.py --dry-run         # preview only, no changes
    python dedupe_db_rows.py --base-dir "F:\\Data Media Hoard"  # override
                                                # the folder relative paths
                                                # are resolved against
                                                # (default: the database
                                                # file's own folder)

Always makes a timestamped backup copy of the DB file before writing
(skipped in --dry-run mode, since nothing is written anyway).
"""

from __future__ import annotations

import ntpath
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def _normalize_for_compare(filepath: str, base_dir: str | None = None) -> str:
    """
    Normalized, lowercased form of a Windows-style path, used ONLY to
    decide whether two path strings point at the same file — never
    written back to the database (Windows paths are case-insensitive
    for matching purposes, but the actual file on disk has a specific
    case that should be preserved in what we store).
    """
    normalized = ntpath.normpath(filepath)
    if base_dir and not ntpath.isabs(normalized):
        normalized = ntpath.normpath(ntpath.join(base_dir, normalized))
    return normalized.lower()


def _normalize_for_storage(filepath: str, base_dir: str | None) -> str:
    """
    Normalized form of a path suitable for writing back to the database:
    cleans up "." / ".." / mixed separators and (if base_dir is given and
    the path is relative) makes it absolute — but preserves the original
    case, unlike _normalize_for_compare.
    """
    normalized = ntpath.normpath(filepath)
    if base_dir and not ntpath.isabs(normalized):
        normalized = ntpath.normpath(ntpath.join(base_dir, normalized))
    return normalized


def find_groups_by_resolved_path(rows: list[sqlite3.Row],
                                 base_dir: str | None) -> dict[str, list[sqlite3.Row]]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = _normalize_for_compare(row["filepath"], base_dir)
        groups.setdefault(key, []).append(row)
    return {k: rs for k, rs in groups.items() if len(rs) > 1}


def find_groups_by_name_and_size(rows: list[sqlite3.Row]) -> dict[tuple, list[sqlite3.Row]]:
    """
    Fallback pass: same filename + same file_size, treated as the same
    physical file. Skips rows with no file_size recorded (can't compare).
    """
    groups: dict[tuple, list[sqlite3.Row]] = {}
    for row in rows:
        if row["file_size"] is None:
            continue
        key = (row["filename"], row["file_size"])
        groups.setdefault(key, []).append(row)
    return {k: rs for k, rs in groups.items() if len(rs) > 1}


def pick_best_row(rows: list[sqlite3.Row]) -> sqlite3.Row:
    """
    Choose which row to keep when several point at the same real file.
    Priority: not-pending status > has tags > has hashes > has artist/site
    > absolute path (more reliable than a relative one, which depends on
    working directory) > highest id (newest scan).

    Uses ntpath.isabs rather than pathlib, since the app this database
    belongs to runs on Windows and stores Windows-style paths regardless
    of which OS this cleanup script happens to be run from.
    """
    def score(r: sqlite3.Row) -> tuple:
        return (
            0 if r["upload_status"] == "pending" else 1,
            1 if r["tags"] else 0,
            1 if r["sha256"] else 0,
            1 if (r["artist"] or r["site"]) else 0,
            1 if ntpath.isabs(r["filepath"]) else 0,
            r["id"],
        )
    return max(rows, key=score)


def merge_group(con: sqlite3.Connection, rows: list[sqlite3.Row], dry_run: bool,
                label: str, rewrite_path: bool, base_dir: str | None) -> None:
    keep = pick_best_row(rows)
    losers = [r for r in rows if r["id"] != keep["id"]]

    merged_tags       = keep["tags"]       or next((r["tags"]       for r in losers if r["tags"]),       None)
    merged_sha256     = keep["sha256"]     or next((r["sha256"]     for r in losers if r["sha256"]),     None)
    merged_phash      = keep["phash"]      or next((r["phash"]      for r in losers if r["phash"]),      None)
    merged_artist     = keep["artist"]     or next((r["artist"]     for r in losers if r["artist"]),     None)
    merged_site       = keep["site"]       or next((r["site"]       for r in losers if r["site"]),       None)
    merged_source_url = keep["source_url"] or next((r["source_url"] for r in losers if r["source_url"]), None)
    # Only normalize the filepath when this group was matched BY resolved
    # path in the first place (pass 1) — that's the case where the kept
    # row's string benefits from being cleaned up. For pass-2 (filename+
    # size) matches, the kept row's path was chosen specifically because
    # it's likely already fine; normalizing it here adds risk for no
    # benefit, so it's left exactly as-is.
    new_filepath = (
        _normalize_for_storage(keep["filepath"], base_dir) if rewrite_path
        else keep["filepath"]
    )

    print(f"\n{label}")
    for r in rows:
        marker = "KEEP  " if r["id"] == keep["id"] else "DELETE"
        print(f"  [{marker}] id={r['id']:<6} status={r['upload_status']:<10} "
              f"size={r['file_size']}  path={r['filepath']}")

    if dry_run:
        return

    # Delete the losing rows FIRST. If the survivor's normalized path
    # happens to exactly match what a losing row is still storing (very
    # possible — e.g. the kept row was chosen for having better metadata,
    # not for already having the cleanest path string), updating the
    # survivor before removing the losers would collide with the UNIQUE
    # constraint on filepath. Deleting first means there's nothing left
    # to collide with by the time the UPDATE runs.
    for r in losers:
        con.execute("DELETE FROM artists_media WHERE id = ?", (r["id"],))

    # Defensive check: after removing this group's losers, make sure no
    # OTHER row (from a different group, not yet processed) already owns
    # the path we're about to write. This can happen if grouping missed
    # a match that the database itself would still consider identical.
    # If it happens, skip the rename rather than crash — the row's data
    # (tags/hashes/artist/etc.) was still merged and the duplicate(s) in
    # THIS group still got removed; only the filepath cleanup is skipped.
    if rewrite_path:
        clash = con.execute(
            "SELECT id FROM artists_media WHERE filepath = ? AND id != ?",
            (new_filepath, keep["id"]),
        ).fetchone()
        if clash is not None:
            print(f"  [WARN] skipping path rewrite for id={keep['id']} — "
                  f"id={clash[0]} already owns that exact path string; "
                  f"re-run the script afterward to merge that pair too.")
            new_filepath = keep["filepath"]

    con.execute(
        "UPDATE artists_media SET filepath = ?, tags = ?, sha256 = ?, phash = ?, "
        "artist = ?, site = ?, source_url = ? WHERE id = ?",
        (new_filepath, merged_tags, merged_sha256, merged_phash,
         merged_artist, merged_site, merged_source_url, keep["id"]),
    )


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    base_dir = None
    if "--base-dir" in args:
        idx = args.index("--base-dir")
        base_dir = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    db_path = Path(args[0]) if args else Path("hoard.db")

    if not db_path.exists():
        print(f"Database not found: {db_path}")
        print("Run this from your project root, or pass the path explicitly:")
        print(r'    python dedupe_db_rows.py "F:\Data Media Hoard\hoard.db"')
        sys.exit(1)

    # Relative paths stored in the database (e.g. ".\download\X\...") are
    # relative to whatever the app's working directory was when that row
    # was created — for this app, that's always the project root, i.e.
    # the folder this database file lives in. Default to that unless the
    # person explicitly overrides it with --base-dir.
    if base_dir is None:
        base_dir = str(db_path.parent.resolve())
    print(f"Using base directory for relative paths: {base_dir}")

    if not dry_run:
        backup_path = db_path.with_name(
            f"{db_path.stem}.backup_{datetime.now():%Y%m%d_%H%M%S}{db_path.suffix}"
        )
        shutil.copy(db_path, backup_path)
        print(f"Backup written to: {backup_path}")

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    all_rows = con.execute("SELECT * FROM artists_media").fetchall()

    total_groups = 0
    pass1_deleted_ids: set[int] = set()

    print("\n=== Pass 1: matching by normalized path ===")
    path_groups = find_groups_by_resolved_path(all_rows, base_dir)
    for real_path, rows in path_groups.items():
        keep_id = pick_best_row(rows)["id"]
        merge_group(con, rows, dry_run, label=f"Path: {real_path}",
                   rewrite_path=True, base_dir=base_dir)
        pass1_deleted_ids.update(r["id"] for r in rows if r["id"] != keep_id)
        total_groups += 1

    # Pass 2 must see pass 1's survivors with their POST-merge data (not
    # the stale pre-merge snapshot from all_rows) so an absolute-path row
    # that didn't normalize-match anything can still catch up to whichever
    # row pass 1 already merged the relative-path spellings into. Re-query
    # fresh rather than reuse all_rows — SQLite shows a connection its own
    # uncommitted writes immediately, so this is accurate even in the
    # final non-dry-run commit that happens later.
    print("\n=== Pass 2: matching by filename + file size (fallback) ===")
    if dry_run:
        # Nothing was actually written, so simulate "what pass 2 would see"
        # by excluding rows pass 1 would have deleted.
        remaining = [r for r in all_rows if r["id"] not in pass1_deleted_ids]
    else:
        remaining = con.execute("SELECT * FROM artists_media").fetchall()

    name_size_groups = find_groups_by_name_and_size(remaining)
    for (name, size), rows in name_size_groups.items():
        merge_group(con, rows, dry_run, label=f"Same name + size: {name} ({size} bytes)",
                   rewrite_path=False, base_dir=base_dir)
        total_groups += 1

    if total_groups == 0:
        print("\nNo duplicate rows found — nothing to clean up.")
    elif dry_run:
        print(f"\nWould merge {total_groups} group(s). Re-run without --dry-run to apply.")
    else:
        con.commit()
        print(f"\nDone — merged {total_groups} group(s).")

    con.close()


if __name__ == "__main__":
    main()