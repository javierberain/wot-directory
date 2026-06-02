#!/usr/bin/env python3
"""
scrub_origins.py - One-time origin normalization sweep using directory_rules.

Two legacy problems this fixes in an existing DB:
  1. The "unknown" trap: rows holding a literal placeholder string ('unknown',
     'unclear', ...) are normalized to NULL so they stay eligible for later
     resolution (enrich_character treats NULL as unresolved; it treated the
     literal 'unknown' as filled).
  2. Demonyms / hedges: 'Andoran' -> 'Andor', 'Tear (presumably)' -> 'Tear',
     per the README Nationality conventions, now enforced by
     directory_rules.normalize_nationality.

Only rows whose normalized value DIFFERS from the stored value are touched.
Every change is appended to data/origins_scrubbed.csv (db_file, character_id,
primary_name, old_value, new_value, rationale).

Dry-run by default; --commit writes; a backup is taken before the first write.

    python scripts/scrub_origins.py --db db/wot.db            # dry-run
    python scripts/scrub_origins.py --db db/wot.db --commit
"""
import argparse
import csv
import os
import pathlib
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
from directory_rules import normalize_nationality, is_unresolved_origin  # noqa

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "wot.db")
CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data",
                        "origins_scrubbed.csv")


def take_backup(db_path):
    p = pathlib.Path(db_path).resolve()
    bak = pathlib.Path(str(p) + ".pre-scrub-origins.bak")
    shutil.copy2(p, bak)
    for ext in ("-wal", "-shm"):
        s = pathlib.Path(str(p) + ext)
        if s.exists():
            shutil.copy2(s, pathlib.Path(str(bak) + ext))
    print(f"Backup written: {bak}")


def log_rows(db_file, changes):
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    new = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["db_file", "character_id", "primary_name",
                        "old_value", "new_value", "rationale"])
        for cid, pname, old, new_val in changes:
            rationale = ("Scrub placeholder origin to NULL (was treated as "
                         "filled, blocking later resolution)."
                         if is_unresolved_origin(old)
                         else "Normalize origin per Nationality conventions "
                              "(place not demonym / strip hedge).")
            w.writerow([db_file, cid, pname, old,
                        "" if new_val is None else new_val, rationale])


def run(db_path, commit):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT character_id, primary_name, nationality FROM characters "
        "WHERE nationality IS NOT NULL AND TRIM(nationality) <> ''"
    ).fetchall()

    changes = []
    for cid, pname, nat in rows:
        new_val = normalize_nationality(nat)
        if new_val != nat:               # None (placeholder) or normalized form
            changes.append((cid, pname, nat, new_val))

    print(f"{'DRY RUN' if not commit else 'COMMIT'}: {db_path}")
    print(f"{len(changes)} origin value(s) to normalize "
          f"(of {len(rows)} non-null):\n")
    for cid, pname, old, new_val in changes:
        print(f"  cid={cid:>4}  \"{pname}\"  "
              f"{old!r} -> {('NULL' if new_val is None else repr(new_val))}")

    if not changes:
        conn.close()
        return
    if not commit:
        print("\nDry-run complete. Re-run with --commit to apply.")
        conn.close()
        return

    take_backup(db_path)
    try:
        for cid, _pname, _old, new_val in changes:
            conn.execute(
                "UPDATE characters SET nationality = ?, "
                "updated_at = datetime('now') WHERE character_id = ?",
                (new_val, cid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    log_rows(os.path.basename(db_path), changes)
    print(f"\nCOMMITTED {len(changes)} change(s). Logged to {CSV_PATH}")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    run(args.db, args.commit)


if __name__ == "__main__":
    main()
