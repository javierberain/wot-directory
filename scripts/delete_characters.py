#!/usr/bin/env python3
"""
delete_characters.py - Permanently remove a fixed list of confirmed-bogus
character rows and all their dependent data from wot.db.

IMPORTANT: This script prints a complete dossier of every row that will be
deleted, then requires you to type the word DELETE before it touches anything.
There is no --auto or --force mode. There are no silent deletes.

The target list is hard-coded as TARGET_IDS near the top of this file.
Do not modify that list without first running:

    python scripts/hygiene_audit.py --detail <character_id>

to see exactly what each character_id owns before committing to removal.

Usage:
    python scripts/delete_characters.py
    python scripts/delete_characters.py --db PATH  # target a specific database file;
                                                    # omit to default to db/wot.db
"""

import argparse
import os
import pathlib
import shutil
import sqlite3
import sys


# ── Paths (same conventions as reconcile.py / hygiene_audit.py) ──────────────
DB_PATH  = os.path.join(os.path.dirname(__file__), "..", "db", "wot.db")
BAK_PATH = os.path.join(os.path.dirname(__file__), "..", "db",
                         "wot.db.pre-deletions-auto-book3.bak")


# ── Target list ───────────────────────────────────────────────────────────────
# Confirmed non-character rows approved for deletion after hygiene_audit.py
# review.  These are generic creatures and unnamed crowd-scene placeholders
# that were incorrectly given character rows by the extractor.
#
# Each id was verified with:  python scripts/hygiene_audit.py --detail <id>
#
# Do NOT add ids to this list without running that command first and
# confirming the character has no legitimate dependent data worth keeping.
# Book-1 cleanup: two confirmed non-individual rows verified against chapter text.
#   135  "the raven"                          (Ch34; no 'raven' in text, no real figure)
#   152  "the small black cat with white feet" (Ch42-43; background prop, no individual action)
# The other four B-flags (83, 108, 125, 159) were verified as REAL characters and KEPT.
TARGET_IDS = [135, 152]


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
    # Must be set before any transaction begins; applies for this connection.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def take_backup():
    """Copy wot.db to wot.db.pre-deletions-auto.bak before any writes."""
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


def print_dossier(conn, cid):
    """Print all rows that will be deleted for one character_id.

    Returns True if the character was found, False if it is already absent.
    """
    char = conn.execute(
        "SELECT character_id, primary_name, character_type "
        "FROM characters WHERE character_id = ?",
        (cid,),
    ).fetchone()

    if not char:
        print()
        print(f"  character_id={cid}: NOT FOUND "
              f"(already deleted or not in this database)")
        return False

    print()
    print(f"  character_id={cid}  "
          f"\"{char['primary_name']}\"  [{char['character_type']}]")

    # -- aliases --
    aliases = conn.execute(
        "SELECT alias_id, alias_text, alias_type "
        "FROM aliases WHERE character_id = ? "
        "ORDER BY is_primary DESC, alias_text",
        (cid,),
    ).fetchall()
    print(f"    aliases ({len(aliases)}):")
    for a in aliases:
        print(f"      alias_id={a['alias_id']}  "
              f"\"{a['alias_text']}\"  [{a['alias_type']}]")

    # -- appearances --
    apps = conn.execute("""
        SELECT ap.appearance_id, b.series_order, ch.chapter_number, ch.title
          FROM appearances ap
          JOIN chapters ch ON ch.chapter_id = ap.chapter_id
          JOIN books    b  ON  b.book_id    = ch.book_id
         WHERE ap.character_id = ?
         ORDER BY b.series_order, ch.chapter_number
    """, (cid,)).fetchall()
    print(f"    appearances ({len(apps)}):")
    for ap in apps:
        print(f"      appearance_id={ap['appearance_id']}  "
              f"Bk{ap['series_order']}/Ch{ap['chapter_number']}  "
              f"\"{ap['title']}\"")

    # -- relationships --
    # CASE expression resolves the "other" end of each edge regardless of which
    # column this character's id sits in.  Four bound parameters: see query.
    rels = conn.execute("""
        SELECT r.relationship_id, r.relationship_type, r.directed,
               CASE WHEN r.character_a = ? THEN r.character_b
                    ELSE r.character_a END AS other_id,
               c.primary_name AS other_name
          FROM relationships r
          JOIN characters c
            ON c.character_id = (
               CASE WHEN r.character_a = ? THEN r.character_b
                    ELSE r.character_a END)
         WHERE r.character_a = ? OR r.character_b = ?
         ORDER BY r.relationship_type, c.primary_name
    """, (cid, cid, cid, cid)).fetchall()
    print(f"    relationships ({len(rels)}):")
    for r in rels:
        directed_str = "directed" if r["directed"] else "undirected"
        print(f"      relationship_id={r['relationship_id']}  "
              f"[{r['relationship_type']} / {directed_str}]")
        print(f"        other party: character_id={r['other_id']}"
              f"  \"{r['other_name']}\"")

    # -- character_factions --
    factions = conn.execute("""
        SELECT cf.faction_id, f.name AS faction_name, cf.role
          FROM character_factions cf
          JOIN factions f ON f.faction_id = cf.faction_id
         WHERE cf.character_id = ?
         ORDER BY f.name
    """, (cid,)).fetchall()
    print(f"    faction memberships ({len(factions)}):")
    for f in factions:
        print(f"      faction_id={f['faction_id']}  "
              f"\"{f['faction_name']}\"  role={f['role']}")

    return True


def collect_counts(conn):
    """Return total rows present across all TARGET_IDS, per table.

    Called before deletion so the counts can be reported as rows deleted.
    """
    ph = _placeholders(TARGET_IDS)
    ids = TARGET_IDS

    chars = conn.execute(
        f"SELECT COUNT(*) FROM characters WHERE character_id IN ({ph})",
        ids,
    ).fetchone()[0]

    aliases = conn.execute(
        f"SELECT COUNT(*) FROM aliases WHERE character_id IN ({ph})",
        ids,
    ).fetchone()[0]

    apps = conn.execute(
        f"SELECT COUNT(*) FROM appearances WHERE character_id IN ({ph})",
        ids,
    ).fetchone()[0]

    # Relationships: target may sit on either end of the edge.
    # {ph} appears twice in the query -> pass ids + ids (18 + 18 params).
    rels = conn.execute(
        f"SELECT COUNT(*) FROM relationships "
        f"WHERE character_a IN ({ph}) OR character_b IN ({ph})",
        ids + ids,
    ).fetchone()[0]

    facs = conn.execute(
        f"SELECT COUNT(*) FROM character_factions WHERE character_id IN ({ph})",
        ids,
    ).fetchone()[0]

    return chars, aliases, apps, rels, facs


# ── Deletion (called only after explicit confirmation) ────────────────────────

def delete_all(conn):
    """Delete all TARGET_IDS and their dependents in foreign-key-safe order.

    Caller is responsible for the surrounding transaction (begin / commit /
    rollback).  Order: join tables and dependents first, characters row last.
    This satisfies all FK constraints with PRAGMA foreign_keys = ON.
    """
    ph = _placeholders(TARGET_IDS)
    ids = TARGET_IDS
    
    # 1. character_factions (FK -> characters, FK -> factions)
    n_facs = conn.execute(
        f"DELETE FROM character_factions WHERE character_id IN ({ph})",
        ids,
    ).rowcount

    # 2. appearances (FK -> characters, FK -> chapters)
    n_apps = conn.execute(
        f"DELETE FROM appearances WHERE character_id IN ({ph})",
        ids,
    ).rowcount

    # 3. relationships (character_a and character_b both FK -> characters)
    n_rels = conn.execute(
        f"DELETE FROM relationships "
        f"WHERE character_a IN ({ph}) OR character_b IN ({ph})",
        ids + ids,
    ).rowcount

    # 4. aliases (FK -> characters)
    n_aliases = conn.execute(
        f"DELETE FROM aliases WHERE character_id IN ({ph})",
        ids,
    ).rowcount

    # 5. characters (root row — deleted last)
    n_chars = conn.execute(
        f"DELETE FROM characters WHERE character_id IN ({ph})",
        ids,
    ).rowcount

    return n_chars, n_aliases, n_apps, n_rels, n_facs


def verify_deleted(conn):
    """Return any TARGET_IDS still present in characters after deletion."""
    ph = _placeholders(TARGET_IDS)
    return conn.execute(
        f"SELECT character_id, primary_name FROM characters "
        f"WHERE character_id IN ({ph})",
        TARGET_IDS,
    ).fetchall()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Delete confirmed-bogus character rows from the WoT database.",
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
        BAK_PATH = args.db + ".pre-deletions-auto.bak"

    print()
    print(_SEP2)
    print("  WoT CHARACTER DIRECTORY -- DELETE CHARACTERS")
    print(f"  {len(TARGET_IDS)} character_ids targeted for deletion")
    print(_SEP2)
    print()

    # ── Step 0: backup before anything else ───────────────────────────────────
    take_backup()
    print()

    # ── Open DB read-write (PRAGMA foreign_keys already set by open_db) ───────
    conn = open_db()

    # ── Step 1: print dossier for every target id ─────────────────────────────
    _header("DOSSIER: ROWS TO BE DELETED ")
    print("Every row listed below, across all five tables, will be")
    print("permanently removed from the database on confirmation.")

    not_found = []
    for cid in TARGET_IDS:
        found = print_dossier(conn, cid)
        if not found:
            not_found.append(cid)

    print()

    # ── Step 2: summary totals ────────────────────────────────────────────────
    chars_count, aliases_count, apps_count, rels_count, facs_count = \
        collect_counts(conn)

    _header("SUMMARY: WHAT WILL BE DELETED ")
    print(f"  character rows      : {chars_count}")
    print(f"  aliases rows        : {aliases_count}")
    print(f"  appearances rows    : {apps_count}")
    print(f"  relationships rows  : {rels_count}")
    print(f"  character_factions  : {facs_count}")
    if not_found:
        print()
        print(f"  Note: {len(not_found)} target id(s) not found in this "
              f"database (skipped): {not_found}")
    print()

    if chars_count == 0:
        print("Nothing to delete — all target character_ids are already absent.")
        conn.close()
        return

    # ── Step 3: interactive confirmation ──────────────────────────────────────
    print(_SEP)
    print("This operation is PERMANENT. Rows deleted cannot be recovered")
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

    # ── Step 4: delete inside a single transaction ────────────────────────────
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

    # ── Step 5: verify all target ids are gone ────────────────────────────────
    still_present = verify_deleted(conn)
    print()
    if still_present:
        print("WARNING: the following target ids are still present:")
        for row in still_present:
            print(f"  character_id={row['character_id']}"
                  f"  \"{row['primary_name']}\"")
        print("Investigate before proceeding.")
    else:
        print("Verified: all target character_ids are gone from the database.")

    # ── Step 6: final row-count report (actual rows deleted) ──────────────────
    del_chars, del_aliases, del_apps, del_rels, del_facs = deleted
    print()
    _header("ROWS DELETED (actual) ")
    print(f"  characters          : {del_chars}")
    print(f"  aliases             : {del_aliases}")
    print(f"  appearances         : {del_apps}")
    print(f"  relationships       : {del_rels}")
    print(f"  character_factions  : {del_facs}")
    # Cross-check against the pre-deletion dossier counts.
    if (del_chars, del_aliases, del_apps, del_rels, del_facs) != \
       (chars_count, aliases_count, apps_count, rels_count, facs_count):
        print()
        print("  NOTE: actual deletions differ from the pre-deletion summary.")
        print(f"  pre-deletion summary was: characters={chars_count}, "
              f"aliases={aliases_count}, appearances={apps_count}, "
              f"relationships={rels_count}, character_factions={facs_count}")
        print("  A relationship between two targeted characters is counted")
        print("  once on deletion but can inflate the pre-deletion summary.")
        print("  This is expected and not an error.")

    conn.close()


if __name__ == "__main__":
    main()
