#!/usr/bin/env python3
"""
delete_aliases.py - Permanently remove one or more confirmed-generic alias
rows from a WoT snapshot database.  The underlying character rows are NOT
touched.

IMPORTANT: This script prints a complete dossier of every alias that will be
deleted. In dry-run mode (the default) it makes no changes and takes no backup.
With --commit it takes a backup and then requires you to type the word DELETE
before it touches anything. There is no --auto or --force mode that bypasses
the typed confirmation; there are no silent deletes.

Target alias_ids are supplied on the command line (there is no hard-coded
list). Confirm each alias_id first with:

    python scripts/hygiene_audit.py

so it was flagged by Check A as a generic form of address that pollutes the
matcher.

Generic aliases (like "Aes Sedai", "Captain", "Mother", "the innkeeper")
are NOT individual identities.  Letting them sit in the aliases table
causes the matcher to incorrectly resolve a chapter's mention of any
Aes Sedai to whichever specific character happens to have that string
attached.  Removing the alias rows fixes that bug without affecting the
character rows themselves.

This script REFUSES to delete any alias marked is_primary=1: doing so would
leave its character row unreachable by its canonical name.

Usage:
    # Preview (dry-run) — no writes, no backup:
    python scripts/delete_aliases.py --id 311 --id 116
    python scripts/delete_aliases.py --ids 311 116

    # Apply, against the latest db/wot_book{N}.db snapshot by default:
    python scripts/delete_aliases.py --ids 311 116 --commit

    # Target a specific database file:
    python scripts/delete_aliases.py --ids 311 116 --db db/wot_book3.db --commit
"""

import argparse
import glob
import os
import pathlib
import re
import shutil
import sqlite3
import sys


# ── Paths (same conventions as delete_characters.py) ─────────────────────────
# Resolved at runtime in main(): --db wins, otherwise the latest wot_book*.db
# snapshot. BAK_PATH is derived from the resolved DB, never hard-coded.
DB_PATH  = None
BAK_PATH = None


# ── Formatting helpers (ASCII-only for Windows cp1252 console safety) ─────────
_SEP  = "-" * 60
_SEP2 = "=" * 60


def _header(title):
    print(f"{title:-<60}")


# ── Database helpers ──────────────────────────────────────────────────────────

def discover_latest_db():
    """Return the path to the latest db/wot_book{N}.db snapshot, or None.

    Mirrors hygiene_audit.py's glob-and-max discovery so all the cleanup tools
    default to the same database.
    """
    snaps = []
    db_dir = os.path.join(os.path.dirname(__file__), "..", "db")
    for p in glob.glob(os.path.join(db_dir, "wot_book*.db")):
        m = re.match(r"wot_book(\d+)\.db$", os.path.basename(p))
        if m:
            snaps.append((int(m.group(1)), p))
    if snaps:
        return sorted(snaps)[-1][1]
    return None


def open_db():
    """Open the target DB read-write with foreign-key enforcement on."""
    db_path = pathlib.Path(DB_PATH).resolve()
    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def take_backup():
    """Copy the target DB to its derived .pre-alias-deletions.bak before writes."""
    db_path = pathlib.Path(DB_PATH).resolve()
    bak_path = pathlib.Path(BAK_PATH).resolve()
    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")
    shutil.copy2(db_path, bak_path)
    # Copy WAL/SHM sidecar files if they exist (preserves in-flight state).
    for ext in ("-wal", "-shm"):
        sidecar = pathlib.Path(str(db_path) + ext)
        if sidecar.exists():
            shutil.copy2(sidecar, pathlib.Path(str(bak_path) + ext))
            print(f"  (also backed up WAL sidecar {sidecar.name})")
    print(f"Backup written: {bak_path}")


# ── Dossier helpers ───────────────────────────────────────────────────────────

def _placeholders(ids):
    """Return a SQL IN-list placeholder string for a list of ids."""
    return ",".join("?" * len(ids))


def print_dossier(conn, aid):
    """Print the row that will be deleted for one alias_id.

    Returns True if the alias was found, False if it is already absent, or the
    string "primary" if the alias is marked is_primary=1 (a hard stop).
    """
    row = conn.execute(
        """SELECT a.alias_id, a.alias_text, a.alias_norm, a.alias_type,
                  a.is_primary, a.notes, a.character_id, c.primary_name,
                  c.character_type
             FROM aliases a
             JOIN characters c ON c.character_id = a.character_id
            WHERE a.alias_id = ?""",
        (aid,),
    ).fetchone()

    if not row:
        print()
        print(f"  alias_id={aid}: NOT FOUND "
              f"(already deleted or not in this database)")
        return False

    print()
    print(f"  alias_id={aid}  "
          f"\"{row['alias_text']}\"  [{row['alias_type']}]")
    print(f"    on character_id={row['character_id']}"
          f"  \"{row['primary_name']}\"  [{row['character_type']}]")
    if row['is_primary']:
        # This is the safety wall: never let a primary alias be deleted by
        # this script.  Doing so would orphan the character_id.  Caller
        # checks the return signal and aborts if any flag is hit.
        print(f"    *** WARNING: this is the PRIMARY alias for the "
              f"character.  Deleting it would leave the character row "
              f"unreachable by its primary name.  This script will REFUSE "
              f"to proceed if any target alias is marked primary.")
        return "primary"
    if row['notes']:
        print(f"    notes: {row['notes']}")
    return True


def collect_count(conn, ids):
    """Return the total alias rows present across the target ids.

    Called before deletion so the count can be reported as rows deleted.
    """
    ph = _placeholders(ids)
    return conn.execute(
        f"SELECT COUNT(*) FROM aliases WHERE alias_id IN ({ph})",
        ids,
    ).fetchone()[0]


# ── Deletion (called only after explicit confirmation) ────────────────────────

def delete_all(conn, ids):
    """Delete all target alias_ids from the aliases table.

    Caller is responsible for the surrounding transaction (begin / commit /
    rollback).  Aliases are a leaf table from this script's perspective:
    nothing has a foreign key pointing at aliases.alias_id, so a single
    DELETE statement is safe.
    """
    ph = _placeholders(ids)
    return conn.execute(
        f"DELETE FROM aliases WHERE alias_id IN ({ph})",
        ids,
    ).rowcount


def verify_deleted(conn, ids):
    """Return any target alias_ids still present in aliases after deletion."""
    ph = _placeholders(ids)
    return conn.execute(
        f"SELECT alias_id, alias_text, character_id FROM aliases "
        f"WHERE alias_id IN ({ph})",
        ids,
    ).fetchall()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Delete confirmed-generic alias rows from the WoT database.",
    )
    ap.add_argument(
        "--id", dest="ids_single", action="append", type=int, metavar="ID",
        help="An alias_id to delete. Repeatable: --id 311 --id 116.",
    )
    ap.add_argument(
        "--ids", dest="ids_multi", type=int, nargs="+", metavar="ID",
        help="One or more alias_ids: --ids 311 116.",
    )
    ap.add_argument(
        "--db", metavar="PATH",
        help="Path to the SQLite database file. Defaults to the latest "
             "db/wot_book{N}.db snapshot.",
    )
    ap.add_argument(
        "--commit", action="store_true",
        help="Apply the deletion. Without --commit the script is a dry-run: it "
             "prints the full dossier but makes no changes and takes no backup.",
    )
    args = ap.parse_args()

    # ── Gather target ids from --id and --ids (deduped, order preserved) ───────
    target_ids = []
    for i in (args.ids_single or []) + (args.ids_multi or []):
        if i not in target_ids:
            target_ids.append(i)
    if not target_ids:
        ap.error("provide at least one alias_id via --id or --ids")

    # ── Resolve DB / BAK paths ────────────────────────────────────────────────
    global DB_PATH, BAK_PATH
    db = args.db or discover_latest_db()
    if db is None:
        sys.exit("No database given and no db/wot_book*.db snapshot found.\n"
                 "Pass --db PATH to target a specific database file.")
    DB_PATH = db
    BAK_PATH = db + ".pre-alias-deletions.bak"

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print()
    print(_SEP2)
    print("  WoT CHARACTER DIRECTORY -- DELETE ALIASES")
    print(f"  Mode   : {mode}")
    print(f"  DB     : {pathlib.Path(DB_PATH).resolve()}")
    print(f"  {len(target_ids)} alias_id(s) targeted for deletion")
    print(_SEP2)
    print()

    # ── Open DB read-write ────────────────────────────────────────────────────
    conn = open_db()

    # ── Step 1: print dossier for every target id ─────────────────────────────
    _header("DOSSIER: ALIASES TO BE DELETED ")
    print("Every row listed below in the aliases table will be permanently")
    print("removed on commit.  Character rows are NOT affected.")

    not_found = []
    primary_hits = []
    for aid in target_ids:
        result = print_dossier(conn, aid)
        if result is False:
            not_found.append(aid)
        elif result == "primary":
            primary_hits.append(aid)

    print()

    # ── Step 2: hard refuse if any target is a primary alias ──────────────────
    # Applies in BOTH modes: a primary alias must never be a deletion target,
    # so we stop before even a dry-run can suggest it would proceed.
    if primary_hits:
        print(_SEP)
        print("REFUSING TO PROCEED.")
        print(f"The following alias_id(s) are flagged is_primary=1: "
              f"{primary_hits}")
        print("Deleting a primary alias would leave its character row")
        print("unreachable by its canonical name.  Drop these ids from the")
        print("--id/--ids list, or change the character's primary name first,")
        print("before re-running this script.")
        conn.close()
        sys.exit(1)

    # ── Step 3: summary totals ────────────────────────────────────────────────
    alias_count = collect_count(conn, target_ids)

    _header("SUMMARY: WHAT WILL BE DELETED ")
    print(f"  aliases rows         : {alias_count}")
    if not_found:
        print()
        print(f"  Note: {len(not_found)} target id(s) not found in this "
              f"database (skipped): {not_found}")
    print()

    if alias_count == 0:
        print("Nothing to delete -- all target alias_ids are already absent.")
        conn.close()
        return

    # ── Dry-run exit: no backup, no writes ────────────────────────────────────
    if not args.commit:
        print(_SEP)
        print("  Dry-run complete. No changes made, no backup taken.")
        print("  Re-run with --commit to apply the deletion.")
        conn.close()
        return

    # ── Commit path: backup, then interactive confirmation ────────────────────
    take_backup()
    print()
    print(_SEP)
    print("This operation is PERMANENT.  Aliases deleted cannot be recovered")
    print("except by restoring the backup written above.")
    print()
    print(f"Backup location: {pathlib.Path(BAK_PATH).resolve()}")
    print()

    try:
        answer = input(
            "Type DELETE to proceed, or anything else to abort: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print("Aborted. No changes made.")
        conn.close()
        return

    if answer != "DELETE":
        print("Aborted. No changes made.")
        conn.close()
        return

    # ── Delete inside a single transaction ────────────────────────────────────
    print()
    print("Deleting...")
    try:
        deleted = delete_all(conn, target_ids)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        sys.exit(
            f"\nERROR during deletion: {exc}\n"
            f"All changes rolled back. Database is unchanged.\n"
            f"The backup at {pathlib.Path(BAK_PATH).resolve()} is intact."
        )

    # ── Verify all target ids are gone ────────────────────────────────────────
    still_present = verify_deleted(conn, target_ids)
    print()
    if still_present:
        print("WARNING: the following target ids are still present:")
        for row in still_present:
            print(f"  alias_id={row['alias_id']}"
                  f"  \"{row['alias_text']}\""
                  f"  on character_id={row['character_id']}")
        print("Investigate before proceeding.")
    else:
        print("Verified: all target alias_ids are gone from the database.")

    # ── Final row-count report ────────────────────────────────────────────────
    print()
    _header("ROWS DELETED (actual) ")
    print(f"  aliases              : {deleted}")
    if deleted != alias_count:
        print()
        print(f"  NOTE: actual deletions ({deleted}) differ from the "
              f"pre-deletion summary ({alias_count}).")
        print("  This is unexpected -- investigate.")

    conn.close()


if __name__ == "__main__":
    main()
