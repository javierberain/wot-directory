#!/usr/bin/env python3
"""
seed_disguise_map.py - one-off seeder for the disguise_map registry read by
hygiene_audit.py Check G.

A reveal-disguise is a persona whose true identity is a spoiler (Selene IS
Lanfear; Lord Gaebril IS Rahvin). The directory keeps the persona as a SEPARATE
character row in snapshots BEFORE the reveal book, and MERGES it into the
true-identity row FROM the reveal book onward (persona name kept as an
alias_type='disguise' alias). Because of that merge the persona row no longer
exists post-reveal, so the registry's stable key is the persona's normalised
name (which survives as the disguise alias) plus the stable true-identity cid.

This covers ONLY villain/spoiler reveal-disguises — NOT protagonist travel
cover-names (Moiraine's "Alys", Lan's "Andra"), and NOT dual-identity /
reincarnation cases that coexist post-reveal (Rand/Lews Therin), which are
handled by acknowledged_collisions.

Records ONE persona -> true-identity mapping. Dry-run by default; pass --commit
to write (a backup is taken first). Idempotent via INSERT OR IGNORE on
(persona_norm, true_cid). true_name is derived from true_cid at seed time.

Because start_book.py seeds book N by copying book N-1's snapshot, disguise_map
rows carry forward automatically.

Usage:
    # preview (dry-run): no writes, no backup
    python scripts/seed_disguise_map.py --persona-norm "selene" --true-cid 155 \
        --reveal-book 4 --persona-name "Selene"

    # apply (latest db/wot_book*.db snapshot by default)
    python scripts/seed_disguise_map.py --persona-norm "selene" --true-cid 155 \
        --reveal-book 4 --persona-name "Selene" \
        --note "Selene is Lanfear; revealed in book 4" --commit
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
CREATE TABLE IF NOT EXISTS disguise_map (
    persona_norm  TEXT    NOT NULL,
    true_cid      INTEGER NOT NULL REFERENCES characters(character_id),
    persona_name  TEXT    NOT NULL,
    true_name     TEXT    NOT NULL,
    reveal_book   INTEGER NOT NULL,
    persona_cid   INTEGER,
    note          TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (persona_norm, true_cid)
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
        description="Register one reveal-disguise persona -> true-identity "
                    "mapping in the disguise_map (Check G) registry.")
    ap.add_argument("--persona-norm", required=True,
                    help="persona name; normalised internally the same way "
                         "alias_norm is (so the raw or normalised form works)")
    ap.add_argument("--true-cid", type=int, required=True,
                    help="character_id of the TRUE identity (stable across books)")
    ap.add_argument("--reveal-book", type=int, required=True,
                    help="series_order from which the persona is merged into "
                         "the true identity")
    ap.add_argument("--persona-name", default=None,
                    help="display form of the persona name "
                         "(default: the --persona-norm value as given)")
    ap.add_argument("--persona-cid", type=int, default=None,
                    help="pre-reveal persona row id if known (informational; "
                         "NOT FK-enforced since the row is gone post-reveal)")
    ap.add_argument("--note", default=None,
                    help="short note, e.g. which book reveals the identity")
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

    if args.reveal_book < 1:
        sys.exit("--reveal-book must be a positive series_order.")
    persona_norm = norm(args.persona_norm)
    persona_name = args.persona_name or args.persona_norm

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"Mode : {mode}")
    print(f"DB   : {db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    # The true identity must exist (FK + a readable, denormalised true_name).
    true_name = _char_name(conn, args.true_cid)
    if true_name is None:
        conn.close()
        sys.exit(f"--true-cid {args.true_cid} not found in this DB "
                 f"-- nothing seeded.")

    print(f"  persona      : \"{persona_name}\"  (norm={persona_norm!r})")
    print(f"  true identity: cid={args.true_cid} \"{true_name}\"")
    print(f"  reveal book  : {args.reveal_book}")
    if args.persona_cid is not None:
        print(f"  persona_cid  : {args.persona_cid} (informational)")
    if args.note:
        print(f"  note         : {args.note!r}")

    # Does the table exist, and is this exact mapping already present?
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='disguise_map'").fetchone() is not None
    already = bool(has_table and conn.execute(
        "SELECT 1 FROM disguise_map WHERE persona_norm=? AND true_cid=?",
        (persona_norm, args.true_cid)).fetchone())

    # ── Dry-run: no writes, no backup ─────────────────────────────────────────
    if not args.commit:
        print()
        if already:
            print("Dry-run: this persona/true-identity mapping is ALREADY "
                  "registered. Commit would be a no-op.")
        else:
            print("Dry-run: would register this reveal-disguise mapping. "
                  "Re-run with --commit to write.")
        conn.close()
        return

    # ── Commit: backup, then insert ───────────────────────────────────────────
    bak_path = db_path + ".pre-disguise-map.bak"
    shutil.copy2(db_path, bak_path)
    print(f"Backup written: {bak_path}")

    conn.execute(CREATE_SQL)
    cur = conn.execute(
        "INSERT OR IGNORE INTO disguise_map "
        "(persona_norm, true_cid, persona_name, true_name, reveal_book, "
        " persona_cid, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (persona_norm, args.true_cid, persona_name, true_name,
         args.reveal_book, args.persona_cid, args.note))
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM disguise_map").fetchone()[0]
    conn.close()

    print("  INSERT" if cur.rowcount else "  exists (already registered)")
    print(f"Done. disguise_map now holds {total} row(s).")


if __name__ == "__main__":
    main()
