#!/usr/bin/env python3
"""
seed_acknowledged_collisions.py - one-off seeder for the acknowledged_collisions
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

Inserts ONE row, keyed to the exact collision so genuinely new collisions still
flag. Dry-run by default; pass --commit to write (a backup is taken first).
Idempotent: re-running with --commit is a no-op via INSERT OR IGNORE.

Usage:
    # preview (dry-run): no writes, no backup
    python scripts/seed_acknowledged_collisions.py \
        --owner-cid 110 --other-cid 410 --alias-norm "adine"

    # apply (latest db/wot_book*.db snapshot by default)
    python scripts/seed_acknowledged_collisions.py \
        --owner-cid 110 --other-cid 410 --alias-norm "adine" \
        --note "coincidental homonym, unrelated characters" --commit
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
        description="Acknowledge one Check D identity collision so it stops "
                    "re-flagging on every audit (and in future books).")
    ap.add_argument("--owner-cid", type=int, required=True,
                    help="character_id of the row CARRYING the alias")
    ap.add_argument("--other-cid", type=int, required=True,
                    help="character_id whose primary_name the alias matches")
    ap.add_argument("--alias-norm", required=True,
                    help="the colliding alias text (normalised internally so "
                         "either the raw or normalised form works)")
    ap.add_argument("--note", default=None,
                    help="short reason this collision is acknowledged")
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

    if args.owner_cid == args.other_cid:
        sys.exit("owner-cid and other-cid must differ (a collision is between "
                 "two distinct characters).")
    alias_norm = norm(args.alias_norm)

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"Mode : {mode}")
    print(f"DB   : {db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    # Both characters must exist (FK + a meaningful acknowledgment).
    owner_name = _char_name(conn, args.owner_cid)
    other_name = _char_name(conn, args.other_cid)
    missing = [str(c) for c, n in ((args.owner_cid, owner_name),
                                   (args.other_cid, other_name)) if n is None]
    if missing:
        conn.close()
        sys.exit(f"character_id(s) not found in this DB: {', '.join(missing)} "
                 f"-- nothing acknowledged.")

    print(f"  owner_cid={args.owner_cid} \"{owner_name}\"")
    print(f"  other_cid={args.other_cid} \"{other_name}\"")
    print(f"  alias_norm={alias_norm!r}")
    if args.note:
        print(f"  note={args.note!r}")

    # Does the table exist, and is this exact row already present?
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='acknowledged_collisions'").fetchone() is not None
    already = bool(has_table and conn.execute(
        "SELECT 1 FROM acknowledged_collisions "
        "WHERE owner_cid=? AND other_cid=? AND alias_norm=?",
        (args.owner_cid, args.other_cid, alias_norm)).fetchone())

    # ── Dry-run: no writes, no backup ─────────────────────────────────────────
    if not args.commit:
        print()
        if already:
            print("Dry-run: this collision is ALREADY acknowledged. "
                  "Commit would be a no-op.")
        else:
            print("Dry-run: would acknowledge this collision. "
                  "Re-run with --commit to write.")
        conn.close()
        return

    # ── Commit: backup, then insert ───────────────────────────────────────────
    bak_path = db_path + ".pre-collision-ack.bak"
    shutil.copy2(db_path, bak_path)
    print(f"Backup written: {bak_path}")

    conn.execute(CREATE_SQL)
    cur = conn.execute(
        "INSERT OR IGNORE INTO acknowledged_collisions "
        "(owner_cid, other_cid, alias_norm, note) VALUES (?, ?, ?, ?)",
        (args.owner_cid, args.other_cid, alias_norm, args.note))
    conn.commit()

    total = conn.execute(
        "SELECT COUNT(*) FROM acknowledged_collisions").fetchone()[0]
    conn.close()

    print("  INSERT" if cur.rowcount else "  exists (already acknowledged)")
    print(f"Done. acknowledged_collisions now holds {total} row(s).")


if __name__ == "__main__":
    main()
