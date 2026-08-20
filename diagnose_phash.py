"""
diagnose_phash.py
------------------
Run this against your real hoard.db to see exactly what's stored, so we
can tell whether near-duplicate detection is failing because of missing
data vs. a threshold/logic problem.

Usage:
    python diagnose_phash.py "F:\\Data Media Hoard\\hoard.db"
"""
import sqlite3
import sys
from pathlib import Path


def hamming(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return -1


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else "hoard.db"
    if not Path(db_path).exists():
        print(f"Not found: {db_path}")
        return

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    total = con.execute("SELECT COUNT(*) FROM artists_media").fetchone()[0]
    has_sha = con.execute(
        "SELECT COUNT(*) FROM artists_media WHERE sha256 IS NOT NULL"
    ).fetchone()[0]
    has_phash = con.execute(
        "SELECT COUNT(*) FROM artists_media WHERE phash IS NOT NULL"
    ).fetchone()[0]
    sha_no_phash = con.execute(
        "SELECT COUNT(*) FROM artists_media WHERE sha256 IS NOT NULL AND phash IS NULL"
    ).fetchone()[0]

    print(f"Total rows:                {total}")
    print(f"Rows with sha256:          {has_sha}")
    print(f"Rows with phash:           {has_phash}")
    print(f"sha256 set but NO phash:   {sha_no_phash}  <- these will NEVER be")
    print("                              rehashed unless media_scan.py has the")
    print("                              needs_hashing() fix AND you click Scan")
    print("                              again after replacing the file.")
    print()

    # Show a sample of phash values to check format/length consistency
    rows = con.execute(
        "SELECT filepath, phash FROM artists_media WHERE phash IS NOT NULL LIMIT 10"
    ).fetchall()
    print("Sample phash values (should all be 16 hex chars):")
    for r in rows:
        p = r["phash"]
        flag = "" if p and len(p) == 16 else "  <-- UNEXPECTED LENGTH/FORMAT"
        print(f"  {p}  ({len(p) if p else 0} chars){flag}  {Path(r['filepath']).name}")
    print()

    # Find the closest non-identical pairs among all hashed rows —
    # this tells us what your ACTUAL near-duplicates measure at,
    # so we know if the threshold is the problem.
    all_rows = con.execute(
        "SELECT filepath, phash, sha256 FROM artists_media WHERE phash IS NOT NULL"
    ).fetchall()
    print(f"Computing pairwise distances across {len(all_rows)} hashed files "
          f"(this may take a moment for large libraries)...")

    pairs = []
    n = len(all_rows)
    if n > 3000:
        print(f"  (skipping — {n} files is too many for an O(n^2) scan in this "
              f"diagnostic; sampling first 1500)")
        all_rows = all_rows[:1500]
        n = len(all_rows)

    for i in range(n):
        for j in range(i + 1, n):
            if all_rows[i]["sha256"] == all_rows[j]["sha256"]:
                continue
            d = hamming(all_rows[i]["phash"], all_rows[j]["phash"])
            if d >= 0:
                pairs.append((d, all_rows[i]["filepath"], all_rows[j]["filepath"]))

    pairs.sort(key=lambda x: x[0])
    print()
    print("20 closest non-identical pairs (by phash Hamming distance):")
    for d, a, b in pairs[:20]:
        print(f"  dist={d:3d}   {Path(a).name}  <->  {Path(b).name}")

    if not pairs:
        print("  (no comparable pairs found)")

    con.close()


if __name__ == "__main__":
    main()
