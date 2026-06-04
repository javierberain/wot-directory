#!/usr/bin/env python3
"""
seed_distinct_pairs.py - seeder for the distinct_pairs suppression table read by
hygiene_audit.py Check E.

distinct_pairs records a human-confirmed "these are different people" character
pair so Check E (fuzzy near-duplicate primary names) stops re-flagging them on
every future pass — mirroring how Check E already skips alias-linked pairs.

Inserts ONE pair. The two ids are normalised so the smaller becomes cid_low (the
table also enforces CHECK (cid_low < cid_high)), so the order on the command
line does not matter. Dry-run by default; pass --commit to write (a backup is
taken first). Idempotent: re-running with --commit is a no-op via INSERT OR
IGNORE.

The original 17-pair historical batch (already applied to the live snapshots)
is preserved in scripts/seed_distinct_pairs_initial.py.

Usage:
    # preview (dry-run): no writes, no backup
    python scripts/seed_distinct_pairs.py --cid-low 446 --cid-high 729

    # apply (latest db/wot_book*.db snapshot by default)
    python scripts/seed_distinct_pairs.py --cid-low 446 --cid-high 729 \
        --note "Foo vs Bar, distinct" --commit
"""
import argparse
import glob
import os
import re
import shutil
import sqlite3
import sys

HERE = os.path.dirname(__file__)

# Same definition as db/schema.sql and hygiene_audit.py; created here too so the
# seeder is self-contained on a snapshot that predates the table.
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS distinct_pairs (
    cid_low    INTEGER NOT NULL REFERENCES characters(character_id),
    cid_high   INTEGER NOT NULL REFERENCES characters(character_id),
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (cid_low, cid_high),
    CHECK (cid_low < cid_high)
)
"""


def discover_latest_db():
    """Return the path to the latest db/wot_book{N}.db snapshot, or None.

    Mirrors hygiene_audit.py's glob-and-max discovery so the cleanup tools
    default to the same database.
    """
    snaps = []
    db_dir = os.path.join(HERE, "..", "db")
    for p in glob.glob(os.path.join(db_dir, "wot_book*.db")):
        m = re.match(r"wot_book(\d+)\.db$", os.path.basename(p))
        if m:
            snaps.append((int(m.group(1)), p))
    if snaps:
        return sorted(snaps)[-1][1]
    return None


def _char_name(conn, cid):
    row = conn.execute(
        "SELECT primary_name FROM characters WHERE character_id = ?", (cid,)
    ).fetchone()
    return row[0] if row else None


def main():
    ap = argparse.ArgumentParser(
        description="Seed one confirmed-distinct pair into the distinct_pairs "
                    "Check E suppression table.")
    ap.add_argument("--cid-low", type=int, required=True,
                    help="one character_id of the confirmed-distinct pair")
    ap.add_argument("--cid-high", type=int, required=True,
                    help="the other character_id (order does not matter; the "
                         "smaller is stored as cid_low)")
    ap.add_argument("--note", default=None,
                    help="short reason the pair is confirmed distinct")
    ap.add_argument("--db", default=None,
                    help="SQLite DB to seed (default: latest db/wot_book*.db)")
    ap.add_argument("--commit", action="store_true",
                    help="apply the insert. Without --commit this is a dry-run: "
                         "no writes, no backup.")
    args = ap.parse_args()

    db = args.db or discover_latest_db()
    if db is None:
        sys.exit("No database given and no db/wot_book*.db snapshot found.\n"
                 "Pass --db PATH to target a specific database file.")
    db_path = os.path.abspath(db)
    if not os.path.exists(db_path):
        sys.exit(f"Database not found: {db_path}")

    if args.cid_low == args.cid_high:
        sys.exit("--cid-low and --cid-high must differ (a distinct pair is two "
                 "different characters).")
    # Normalise so the smaller id is cid_low (the table also enforces this), so
    # the user can pass them in either order.
    low, high = sorted((args.cid_low, args.cid_high))

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"Mode : {mode}")
    print(f"DB   : {db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    # Both characters must exist (FK + a readable, meaningful confirmation).
    low_name = _char_name(conn, low)
    high_name = _char_name(conn, high)
    missing = [str(c) for c, n in ((low, low_name), (high, high_name))
               if n is None]
    if missing:
        conn.close()
        sys.exit(f"character_id(s) not found in this DB: {', '.join(missing)} "
                 f"-- nothing seeded.")

    print(f"  cid_low={low} \"{low_name}\"")
    print(f"  cid_high={high} \"{high_name}\"")
    if args.note:
        print(f"  note={args.note!r}")

    # Does the table exist, and is this exact pair already present?
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='distinct_pairs'").fetchone() is not None
    already = bool(has_table and conn.execute(
        "SELECT 1 FROM distinct_pairs WHERE cid_low=? AND cid_high=?",
        (low, high)).fetchone())

    # ── Dry-run: no writes, no backup ─────────────────────────────────────────
    if not args.commit:
        print()
        if already:
            print("Dry-run: this pair is ALREADY recorded as distinct. "
                  "Commit would be a no-op.")
        else:
            print("Dry-run: would record this pair as distinct. "
                  "Re-run with --commit to write.")
        conn.close()
        return

    # ── Commit: backup, then insert ───────────────────────────────────────────
    bak_path = db_path + ".pre-distinct-seed.bak"
    shutil.copy2(db_path, bak_path)
    print(f"Backup written: {bak_path}")

    conn.execute(CREATE_SQL)
    cur = conn.execute(
        "INSERT OR IGNORE INTO distinct_pairs (cid_low, cid_high, note) "
        "VALUES (?, ?, ?)", (low, high, args.note))
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM distinct_pairs").fetchone()[0]
    conn.close()

    print("  INSERT" if cur.rowcount else "  exists (already recorded)")
    print(f"Done. distinct_pairs now holds {total} pair(s).")


if __name__ == "__main__":
    main()
