#!/usr/bin/env python3
"""
seed_distinct_pairs.py - one-off seeder for the distinct_pairs suppression
table read by hygiene_audit.py Check E.

Inserts a fixed list of human-confirmed "these are different people" character
pairs so Check E (fuzzy near-duplicate primary names) stops re-flagging them on
every future pass — mirroring how Check E already skips alias-linked pairs.

Each pair is normalised so the smaller character_id is stored as cid_low (the
table also enforces CHECK (cid_low < cid_high)). Inserts use INSERT OR IGNORE,
so the script is idempotent: re-running neither errors nor duplicates. A backup
of the DB is taken first.

Usage:
    python scripts/seed_distinct_pairs.py                      # db/wot_book5.db
    python scripts/seed_distinct_pairs.py --db db/wot_book5.db
"""
import argparse
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(__file__)
DEFAULT_DB = os.path.join(HERE, "..", "db", "wot_book5.db")

# Confirmed-distinct pairs: (cid_a, cid_b, note). Order within a pair is
# irrelevant here — the inserter normalises so the smaller id becomes cid_low.
# NOTE: the empty-shell rows (Dalar 296, Deain 307, Maric 249) are intentionally
# present ONLY as the other side of a confirmed pair, never seeded on their own.
PAIRS = [
    (413, 487, "Coline Andor vs Colline Aiel"),
    (206, 670, "Maigan (wife of Admer Nem) vs Marigan Ghealdan widow"),
    (206, 486, "Maigan vs Maigran Aiel sister of Lewin"),
    (590, 489, "Garan vs Gearan, distinct Aiel"),
    (595, 498, "Sulin the Maiden vs Sulwin ancestral Tuatha'an leader"),
    (104, 514, "Wil young Deven Ride cousin vs Wit balding farmer, Bk4Ch32"),
    (297, 296, "Alar Ogier vs Dalar"),
    (103, 483, "Aram Tuatha'an vs Garam"),
    (616, 615, "Bari vs Barit Atha'an Miere"),
    (223, 497, "Carn vs Charn ancestral Aiel"),
    (111, 307, "Dain (Bornhald) vs Deain"),
    (510, 263, "Ihvon Warder vs Ivon Cairhien"),
    (495, 601, "Jonai ancestral Aiel vs Joni Andor"),
    (457, 240, "Lian roofmistress vs Lidan Cairhien"),
    (625, 367, "Maira vs Mara"),
    (68, 249, "Mari Baerlon serving woman vs Maric vision-child"),
    (119, 609, "Strom bouncer vs Trom"),
]

# Same definition as db/schema.sql; created here too so the seeder is
# self-contained on a snapshot that predates the table.
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


def main():
    ap = argparse.ArgumentParser(
        description="Seed the distinct_pairs Check E suppression table.")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help="SQLite DB to seed (default: db/wot_book5.db)")
    args = ap.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.exists(db_path):
        sys.exit(f"Database not found: {db_path}")

    # ── Backup first ──────────────────────────────────────────────────────────
    bak_path = db_path + ".pre-distinct-seed.bak"
    shutil.copy2(db_path, bak_path)
    print(f"Backup written: {bak_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(CREATE_SQL)
    conn.commit()

    # Existing character ids, so a pair referencing a missing cid is reported
    # and skipped rather than crashing on a foreign-key failure.
    existing = {row[0] for row in conn.execute(
        "SELECT character_id FROM characters")}

    inserted = already = skipped = 0
    for a, b, note in PAIRS:
        missing = [c for c in (a, b) if c not in existing]
        if missing:
            print(f"  SKIP   ({a},{b}) {note}"
                  f"  -- missing character_id(s): "
                  f"{', '.join(str(m) for m in missing)}")
            skipped += 1
            continue
        low, high = (a, b) if a < b else (b, a)
        cur = conn.execute(
            "INSERT OR IGNORE INTO distinct_pairs (cid_low, cid_high, note) "
            "VALUES (?, ?, ?)", (low, high, note))
        if cur.rowcount:
            print(f"  INSERT ({low},{high}) {note}")
            inserted += 1
        else:
            print(f"  exists ({low},{high}) {note}")
            already += 1
    conn.commit()

    total = conn.execute(
        "SELECT COUNT(*) FROM distinct_pairs").fetchone()[0]
    conn.close()

    print()
    print(f"Done. inserted={inserted}  already-present={already}  "
          f"skipped(missing cid)={skipped}")
    print(f"distinct_pairs now holds {total} pair(s).")


if __name__ == "__main__":
    main()
