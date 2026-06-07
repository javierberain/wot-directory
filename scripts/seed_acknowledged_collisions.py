#!/usr/bin/env python3
"""
seed_acknowledged_collisions.py - seeder for the acknowledged_collisions
suppression table read by hygiene_audit.py Check D.

Check D flags any alias (is_primary=0) whose normalised text equals a DIFFERENT
character's primary_name. Many are permanent, already-reviewed keeps: kept
disguise aliases (e.g. "Aran'gar" on Halima Saranov), canonical dual-identity
aliases ("the Dark One" on Elan Morin Tedronai, "Mandarb" on Zarine Bashere),
and coincidental homonyms where a mononym given_name alias matches an unrelated
character's primary. Recording a collision here stops Check D re-flagging that
exact (owner, other, alias) on every future run.

Because start_book.py seeds book N from book N-1 by copying the snapshot, an
acknowledgment carries forward automatically: a collision acknowledged on book 6
will not re-flag when book 7 is ingested.

Two ways to seed:
  --known : seed the built-in KNOWN_COLLISIONS batch below. Entries are keyed by
            primary_name so a re-seed (or running against an earlier book)
            resolves them to that snapshot's ids and SKIPS characters that don't
            exist there. This is what makes the known suppressions survive a
            re-seed and carry to books 1-8.
  single  : --owner-cid/--other-cid/--alias-norm for an ad-hoc acknowledgment.

Dry-run by default; pass --commit to write (a backup is taken first). Idempotent
via INSERT OR IGNORE on (owner_cid, other_cid, alias_norm).

Usage:
    python scripts/seed_acknowledged_collisions.py --known            # dry-run
    python scripts/seed_acknowledged_collisions.py --known --commit
    python scripts/seed_acknowledged_collisions.py \
        --owner-cid 110 --other-cid 410 --alias-norm "adine" --commit
"""
import argparse
import glob
import os
import re
import shutil
import sqlite3
import sys

from directory_rules import norm

HERE = os.path.dirname(__file__)

# Same definition as db/schema.sql and hygiene_audit.py; created here too so the
# seeder is self-contained on a snapshot that predates the table.
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS acknowledged_collisions (
    owner_cid  INTEGER NOT NULL REFERENCES characters(character_id),
    other_cid  INTEGER NOT NULL REFERENCES characters(character_id),
    alias_norm TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (owner_cid, other_cid, alias_norm)
)
"""

_SLAYER_NOTE = ("Luc and Isam: two origins fused in one body; cross-referenced "
                "on purpose. Confirmed while processing book 9.")

# Built-in, reproducible acknowledged collisions, referenced by primary_name
# (NOT by id) so they resolve correctly in any snapshot and skip books where the
# characters don't yet exist. Seed with --known.
KNOWN_COLLISIONS = [
    # Slayer: the Isam/Luc dual identity cross-references each other on purpose.
    {"owner": "Isam Mandragoran", "other": "Luc Mantear",
     "alias_norm": "luc mantear", "note": _SLAYER_NOTE},
    {"owner": "Luc Mantear", "other": "Isam Mandragoran",
     "alias_norm": "isam mandragoran", "note": _SLAYER_NOTE},
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
    """Return (entries, skipped). entries: list of
    (owner_cid, other_cid, alias_norm, note, label). skipped: list of
    (label, reason) for known entries whose characters aren't in this DB."""
    entries, skipped = [], []
    if args.known:
        for k in KNOWN_COLLISIONS:
            label = f"{k['owner']} / {k['other']} :: {k['alias_norm']}"
            oc = _cid_for_name(conn, k["owner"])
            ot = _cid_for_name(conn, k["other"])
            miss = [n for n, cid in ((k["owner"], oc), (k["other"], ot))
                    if cid is None]
            if miss:
                skipped.append((label, f"not in this DB: {', '.join(miss)}"))
                continue
            entries.append((oc, ot, norm(k["alias_norm"]), k["note"], label))
        return entries, skipped

    # single-entry mode
    if args.owner_cid is None or args.other_cid is None or not args.alias_norm:
        sys.exit("Provide --owner-cid, --other-cid and --alias-norm, "
                 "or use --known.")
    if args.owner_cid == args.other_cid:
        sys.exit("owner-cid and other-cid must differ (a collision is between "
                 "two distinct characters).")
    on = _name_for_cid(conn, args.owner_cid)
    tn = _name_for_cid(conn, args.other_cid)
    miss = [str(c) for c, n in ((args.owner_cid, on), (args.other_cid, tn))
            if n is None]
    if miss:
        sys.exit(f"character_id(s) not found in this DB: {', '.join(miss)} "
                 f"-- nothing acknowledged.")
    label = f"{on} / {tn} :: {args.alias_norm}"
    entries.append((args.owner_cid, args.other_cid, norm(args.alias_norm),
                    args.note, label))
    return entries, skipped


def main():
    ap = argparse.ArgumentParser(
        description="Acknowledge Check D identity collisions so they stop "
                    "re-flagging on every audit (and in future books).")
    ap.add_argument("--known", action="store_true",
                    help="seed the built-in KNOWN_COLLISIONS batch (by name) "
                         "instead of a single --owner/--other/--alias entry")
    ap.add_argument("--owner-cid", type=int,
                    help="character_id of the row CARRYING the alias")
    ap.add_argument("--other-cid", type=int,
                    help="character_id whose primary_name the alias matches")
    ap.add_argument("--alias-norm",
                    help="the colliding alias text (normalised internally)")
    ap.add_argument("--note", default=None,
                    help="short reason this collision is acknowledged")
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
    for oc, ot, an, note, label in entries:
        print(f"  {label}  (owner_cid={oc}, other_cid={ot}, alias_norm={an!r})")

    if not args.commit:
        print()
        print(f"Dry-run: would acknowledge {len(entries)} collision(s) "
              f"(skipped {len(skipped)}). Re-run with --commit to write.")
        conn.close()
        return

    bak_path = db_path + ".pre-collision-ack.bak"
    shutil.copy2(db_path, bak_path)
    print(f"Backup written: {bak_path}")

    conn.execute(CREATE_SQL)
    inserted = already = 0
    for oc, ot, an, note, label in entries:
        cur = conn.execute(
            "INSERT OR IGNORE INTO acknowledged_collisions "
            "(owner_cid, other_cid, alias_norm, note) VALUES (?, ?, ?, ?)",
            (oc, ot, an, note))
        if cur.rowcount:
            inserted += 1
        else:
            already += 1
    conn.commit()
    total = conn.execute(
        "SELECT COUNT(*) FROM acknowledged_collisions").fetchone()[0]
    conn.close()

    print(f"Done. inserted={inserted}  already-present={already}  "
          f"skipped(missing)={len(skipped)}")
    print(f"acknowledged_collisions now holds {total} row(s).")


if __name__ == "__main__":
    main()
