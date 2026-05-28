#!/usr/bin/env python3
"""
delete_aliases.py - Permanently remove a fixed list of confirmed-generic
alias rows from wot.db.  The underlying character rows are NOT touched.

IMPORTANT: This script prints a complete dossier of every alias that will
be deleted, then requires you to type the word DELETE before it touches
anything.  There is no --auto or --force mode.  There are no silent deletes.

The target list is hard-coded as TARGET_ALIAS_IDS near the top of this file.
Do not modify that list without first running:

    python scripts/hygiene_audit.py

to confirm each alias_id was flagged by Check A as a generic form of
address that pollutes the matcher.

Generic aliases (like "Aes Sedai", "Captain", "Mother", "the innkeeper")
are NOT individual identities.  Letting them sit in the aliases table
causes the matcher to incorrectly resolve a chapter's mention of any
Aes Sedai to whichever specific character happens to have that string
attached.  Removing the alias rows fixes that bug without affecting the
character rows themselves.

Usage:
    python scripts/delete_aliases.py
    python scripts/delete_aliases.py --db PATH  # target a specific database file;
                                                # omit to default to db/wot.db
"""

import argparse
import os
import pathlib
import shutil
import sqlite3
import sys


# ── Paths (same conventions as delete_characters.py) ─────────────────────────
DB_PATH  = os.path.join(os.path.dirname(__file__), "..", "db", "wot.db")
BAK_PATH = os.path.join(os.path.dirname(__file__), "..", "db",
                         "wot.db.pre-alias-deletions.bak")


# ── Target list ───────────────────────────────────────────────────────────────
# Generic-alias rows approved for deletion after hygiene_audit.py Check A
# review.  These are forms of address (titles, epithets) whose entire text
# is generic and should not be a name attached to any single character.
#
# Each alias_id was confirmed with the Check A output of hygiene_audit.py.
#
# Do NOT add ids to this list without first re-running hygiene_audit.py
# and confirming the alias still appears in Check A's flagged list.
TARGET_ALIAS_IDS = [
    311,  # "Aes Sedai"     on character_id=129  "Elaida"
    116,  # "Aes Sedai"     on character_id=25   "Moiraine"
    146,  # "child"         on character_id=14   "Egwene"
    365,  # "King"          on character_id=168  "al'Akir Mandragoran"
    309,  # "Mother"        on character_id=107  "Queen Morgase"
    305,  # "my Lady"       on character_id=130  "Elayne"
    306,  # "my Lord"       on character_id=131  "Gawyn"
    233,  # "Queen"         on character_id=107  "Queen Morgase"
    372,  # "Queen"         on character_id=172  "el'Leanna"
    342,  # "the innkeeper" on character_id=136  "Basel Gill"
    270,  # "the Queen"     on character_id=107  "Queen Morgase"
]


# ── Formatting helpers (ASCII-only for Windows cp1252 console safety) ─────────
_SEP  = "-" * 60
_SEP2 = "=" * 60


def _header(title):
    print(f"{title:-<60}")


# ── Database helpers ──────────────────────────────────────────────────────────

def open_db():
    """Open wot.db read-write with foreign-key enforcement on."""
    db_path = pathlib.Path(DB_PATH).resolve()
    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def take_backup():
    """Copy wot.db to wot.db.pre-alias-deletions.bak before any writes."""
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

    Returns True if the alias was found, False if it is already absent.
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


def collect_count(conn):
    """Return the total alias rows present across all TARGET_ALIAS_IDS.

    Called before deletion so the count can be reported as rows deleted.
    """
    ph = _placeholders(TARGET_ALIAS_IDS)
    return conn.execute(
        f"SELECT COUNT(*) FROM aliases WHERE alias_id IN ({ph})",
        TARGET_ALIAS_IDS,
    ).fetchone()[0]


# ── Deletion (called only after explicit confirmation) ────────────────────────

def delete_all(conn):
    """Delete all TARGET_ALIAS_IDS from the aliases table.

    Caller is responsible for the surrounding transaction (begin / commit /
    rollback).  Aliases are a leaf table from this script's perspective:
    nothing has a foreign key pointing at aliases.alias_id, so a single
    DELETE statement is safe.
    """
    ph = _placeholders(TARGET_ALIAS_IDS)
    return conn.execute(
        f"DELETE FROM aliases WHERE alias_id IN ({ph})",
        TARGET_ALIAS_IDS,
    ).rowcount


def verify_deleted(conn):
    """Return any TARGET_ALIAS_IDS still present in aliases after deletion."""
    ph = _placeholders(TARGET_ALIAS_IDS)
    return conn.execute(
        f"SELECT alias_id, alias_text, character_id FROM aliases "
        f"WHERE alias_id IN ({ph})",
        TARGET_ALIAS_IDS,
    ).fetchall()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Delete confirmed-generic alias rows from the WoT database.",
    )
    ap.add_argument(
        "--db", metavar="PATH",
        help="Path to the SQLite database file. "
             "Defaults to db/wot.db (the live ingestion database).",
    )
    args = ap.parse_args()

    # ── Resolve DB/BAK paths from --db if given ───────────────────────────────
    if args.db is not None:
        global DB_PATH, BAK_PATH
        DB_PATH = args.db
        BAK_PATH = args.db + ".pre-alias-deletions.bak"

    print()
    print(_SEP2)
    print("  WoT CHARACTER DIRECTORY -- DELETE ALIASES")
    print(f"  {len(TARGET_ALIAS_IDS)} alias_ids targeted for deletion")
    print(_SEP2)
    print()

    # ── Step 0: backup before anything else ───────────────────────────────────
    take_backup()
    print()

    # ── Open DB read-write ────────────────────────────────────────────────────
    conn = open_db()

    # ── Step 1: print dossier for every target id ─────────────────────────────
    _header("DOSSIER: ALIASES TO BE DELETED ")
    print("Every row listed below in the aliases table will be permanently")
    print("removed on confirmation.  Character rows are NOT affected.")

    not_found = []
    primary_hits = []
    for aid in TARGET_ALIAS_IDS:
        result = print_dossier(conn, aid)
        if result is False:
            not_found.append(aid)
        elif result == "primary":
            primary_hits.append(aid)

    print()

    # ── Step 2: hard refuse if any target is a primary alias ──────────────────
    if primary_hits:
        print(_SEP)
        print("REFUSING TO PROCEED.")
        print(f"The following alias_id(s) are flagged is_primary=1: "
              f"{primary_hits}")
        print("Deleting a primary alias would leave its character row")
        print("unreachable by its canonical name.  Remove these ids from")
        print("TARGET_ALIAS_IDS, or change the character's primary name")
        print("first, before re-running this script.")
        conn.close()
        sys.exit(1)

    # ── Step 3: summary totals ────────────────────────────────────────────────
    alias_count = collect_count(conn)

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

    # ── Step 4: interactive confirmation ──────────────────────────────────────
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

    # ── Step 5: delete inside a single transaction ────────────────────────────
    print()
    print("Deleting...")
    try:
        deleted = delete_all(conn)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        sys.exit(
            f"\nERROR during deletion: {exc}\n"
            f"All changes rolled back. Database is unchanged.\n"
            f"The backup at {pathlib.Path(BAK_PATH).resolve()} is intact."
        )

    # ── Step 6: verify all target ids are gone ────────────────────────────────
    still_present = verify_deleted(conn)
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

    # ── Step 7: final row-count report ────────────────────────────────────────
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
