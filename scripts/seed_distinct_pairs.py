#!/usr/bin/env python3
"""
seed_distinct_pairs.py - seeder for the distinct_pairs suppression table read by
hygiene_audit.py Check E.

distinct_pairs records a human-confirmed "these are different people" character
pair so Check E (fuzzy near-duplicate primary names) stops re-flagging them on
every future pass — mirroring how Check E already skips alias-linked pairs.

Two ways to seed:
  --known : seed the built-in KNOWN_PAIRS batch below. Entries are keyed by
            primary_name so a re-seed (or running against an earlier book)
            resolves them to that snapshot's ids and SKIPS characters that don't
            exist there. This is what makes the known pairs survive a re-seed
            and carry to books 1-8.
  single  : --cid-low/--cid-high for an ad-hoc pair (order doesn't matter; the
            smaller id is stored as cid_low, which the table CHECK enforces).

Dry-run by default; pass --commit to write (a backup is taken first). Idempotent
via INSERT OR IGNORE. The original 17-pair historical batch is preserved in
scripts/seed_distinct_pairs_initial.py.

Usage:
    python scripts/seed_distinct_pairs.py --known            # dry-run
    python scripts/seed_distinct_pairs.py --known --commit
    python scripts/seed_distinct_pairs.py --cid-low 446 --cid-high 729 --commit
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

# Built-in, reproducible distinct pairs, referenced by primary_name (NOT by id)
# so they resolve correctly in any snapshot and skip books where the characters
# don't yet exist. Seed with --known.
KNOWN_PAIRS = [
    {"a": "Lan Mandragoran", "b": "Lain Mandragoran",
     "note": ("Lain is Lan's uncle and Isam's father; ~97% surname similarity "
              "is expected, not a misspelling.")},
]


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


def _cid_for_name(conn, name):
    row = conn.execute(
        "SELECT character_id FROM characters WHERE primary_name = ?", (name,)
    ).fetchone()
    return row[0] if row else None


def _name_for_cid(conn, cid):
    row = conn.execute(
        "SELECT primary_name FROM characters WHERE character_id = ?", (cid,)
    ).fetchone()
    return row[0] if row else None


def _build_entries(conn, args):
    """Return (entries, skipped). entries: list of (cid_low, cid_high, note,
    label). skipped: list of (label, reason)."""
    entries, skipped = [], []
    if args.known:
        for k in KNOWN_PAIRS:
            label = f"{k['a']} / {k['b']}"
            ca, cb = _cid_for_name(conn, k["a"]), _cid_for_name(conn, k["b"])
            miss = [n for n, cid in ((k["a"], ca), (k["b"], cb)) if cid is None]
            if miss:
                skipped.append((label, f"not in this DB: {', '.join(miss)}"))
                continue
            if ca == cb:
                skipped.append((label, "both names resolve to the same row"))
                continue
            low, high = sorted((ca, cb))
            entries.append((low, high, k["note"], label))
        return entries, skipped

    # single-pair mode
    if args.cid_low is None or args.cid_high is None:
        sys.exit("Provide --cid-low and --cid-high, or use --known.")
    if args.cid_low == args.cid_high:
        sys.exit("--cid-low and --cid-high must differ (a distinct pair is two "
                 "different characters).")
    low, high = sorted((args.cid_low, args.cid_high))
    ln, hn = _name_for_cid(conn, low), _name_for_cid(conn, high)
    miss = [str(c) for c, n in ((low, ln), (high, hn)) if n is None]
    if miss:
        sys.exit(f"character_id(s) not found in this DB: {', '.join(miss)} "
                 f"-- nothing seeded.")
    entries.append((low, high, args.note, f"{ln} / {hn}"))
    return entries, skipped


def main():
    ap = argparse.ArgumentParser(
        description="Seed confirmed-distinct pairs into the distinct_pairs "
                    "Check E suppression table.")
    ap.add_argument("--known", action="store_true",
                    help="seed the built-in KNOWN_PAIRS batch (by name) instead "
                         "of a single --cid-low/--cid-high pair")
    ap.add_argument("--cid-low", type=int,
                    help="one character_id of the confirmed-distinct pair")
    ap.add_argument("--cid-high", type=int,
                    help="the other character_id (order does not matter)")
    ap.add_argument("--note", default=None,
                    help="short reason the pair is confirmed distinct")
    ap.add_argument("--db", default=None,
                    help="SQLite DB to seed (default: latest db/wot_book*.db)")
    ap.add_argument("--commit", action="store_true",
                    help="apply the insert(s). Without --commit this is a "
                         "dry-run: no writes, no backup.")
    args = ap.parse_args()

    db = args.db or discover_latest_db()
    if db is None:
        sys.exit("No database given and no db/wot_book*.db snapshot found.\n"
                 "Pass --db PATH to target a specific database file.")
    db_path = os.path.abspath(db)
    if not os.path.exists(db_path):
        sys.exit(f"Database not found: {db_path}")

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"Mode : {mode}")
    print(f"DB   : {db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    entries, skipped = _build_entries(conn, args)
    for label, reason in skipped:
        print(f"  SKIP   {label}  ({reason})")
    for low, high, note, label in entries:
        print(f"  {label}  (cid_low={low}, cid_high={high})")

    if not args.commit:
        print()
        print(f"Dry-run: would record {len(entries)} distinct pair(s) "
              f"(skipped {len(skipped)}). Re-run with --commit to write.")
        conn.close()
        return

    bak_path = db_path + ".pre-distinct-seed.bak"
    shutil.copy2(db_path, bak_path)
    print(f"Backup written: {bak_path}")

    conn.execute(CREATE_SQL)
    inserted = already = 0
    for low, high, note, label in entries:
        cur = conn.execute(
            "INSERT OR IGNORE INTO distinct_pairs (cid_low, cid_high, note) "
            "VALUES (?, ?, ?)", (low, high, note))
        if cur.rowcount:
            inserted += 1
        else:
            already += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM distinct_pairs").fetchone()[0]
    conn.close()

    print(f"Done. inserted={inserted}  already-present={already}  "
          f"skipped(missing)={len(skipped)}")
    print(f"distinct_pairs now holds {total} pair(s).")


if __name__ == "__main__":
    main()
