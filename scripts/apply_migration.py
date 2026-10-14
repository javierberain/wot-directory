#!/usr/bin/env python3
"""
apply_migration.py - Apply one db/migrations/NNN_*.sql file to every per-book
snapshot in db/ (wot_{slot}.db), skipping legacy scratch DBs and backups.

Dry-run by default: prints which snapshots would be migrated and which are
already migrated. --commit applies, creating <db>.pre-migration-NNN.bak first.
Idempotency is detected per migration by a probe query declared below; a
snapshot where the probe succeeds is skipped.

    python scripts/apply_migration.py --migration 004
    python scripts/apply_migration.py --migration 004 --commit
"""
import argparse, glob, os, shutil, sqlite3, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from directory_rules import slot_from_filename  # noqa: E402

DB_DIR = os.path.join(HERE, "..", "db")
MIG_DIR = os.path.join(DB_DIR, "migrations")

# migration number -> SQL that succeeds only if the migration is already applied
ALREADY_APPLIED_PROBE = {
    "004": "SELECT slot, label FROM books LIMIT 1",
}


def find_migration(number):
    hits = glob.glob(os.path.join(MIG_DIR, f"{number}_*.sql"))
    if len(hits) != 1:
        sys.exit(f"expected exactly one migration {number}_*.sql, found {hits}")
    return hits[0]


def snapshots():
    out = []
    for p in sorted(glob.glob(os.path.join(DB_DIR, "wot_*.db"))):
        slot = slot_from_filename(p)
        if slot:
            out.append((slot, p))
    return out


def already_applied(conn, number):
    probe = ALREADY_APPLIED_PROBE.get(number)
    if probe is None:
        return False
    try:
        conn.execute(probe).fetchone()
        return True
    except sqlite3.OperationalError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--migration", required=True, help="e.g. 004")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--only", help="apply to this slot only")
    a = ap.parse_args()
    path = find_migration(a.migration)
    sql = open(path, encoding="utf-8").read()
    print(f"Migration: {os.path.basename(path)}  ({'COMMIT' if a.commit else 'dry run'})")
    for slot, db in snapshots():
        if a.only and slot != a.only:
            continue
        conn = sqlite3.connect(db)
        try:
            if already_applied(conn, a.migration):
                print(f"  {os.path.basename(db):28} already applied, skip")
                continue
            if not a.commit:
                print(f"  {os.path.basename(db):28} WOULD apply")
                continue
            bak = f"{db}.pre-migration-{a.migration}.bak"
            shutil.copy2(db, bak)
            conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
            rows = conn.execute(
                "SELECT series_order, slot, label FROM books ORDER BY series_order").fetchall()
            print(f"  {os.path.basename(db):28} applied; books -> {rows}")
        finally:
            conn.close()
    if not a.commit:
        print("\nDry run only. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
