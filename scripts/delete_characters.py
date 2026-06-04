#!/usr/bin/env python3
"""
delete_characters.py - Permanently remove one or more confirmed-bogus
character rows and all their dependent data from a WoT snapshot database.

IMPORTANT: This script prints a complete dossier of every row that will be
deleted. In dry-run mode (the default) it makes no changes and takes no backup.
With --commit it takes a backup and then requires you to type the word DELETE
before it touches anything. There is no --auto or --force mode that bypasses
the typed confirmation; there are no silent deletes.

Target character_ids are supplied on the command line (there is no hard-coded
list). Verify each id first with:

    python scripts/hygiene_audit.py --detail <character_id>

to see exactly what each character_id owns before committing to removal.

Usage:
    # Preview (dry-run) — no writes, no backup:
    python scripts/delete_characters.py --id 135 --id 152
    python scripts/delete_characters.py --ids 135 152

    # Apply, against the latest db/wot_book{N}.db snapshot by default:
    python scripts/delete_characters.py --ids 135 152 --commit

    # Target a specific database file:
    python scripts/delete_characters.py --ids 135 152 --db db/wot_book3.db --commit
"""

import argparse
import glob
import os
import pathlib
import re
import shutil
import sqlite3
import sys


# ── Paths (same conventions as reconcile.py / hygiene_audit.py) ──────────────
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
    # Must be set before any transaction begins; applies for this connection.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def take_backup():
    """Copy the target DB to its derived .pre-deletions.bak before any writes."""
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


def _has_distinct_pairs(conn):
    """True if the distinct_pairs suppression table exists in this DB.

    The table was added additively (hygiene_audit.py creates/seeds it for the
    Check E near-duplicate suppression). Older snapshots may predate it, so
    every distinct_pairs access below is guarded by this check.
    """
    return conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='distinct_pairs'"
    ).fetchone() is not None


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

    # -- distinct_pairs (Check E suppression; FK on cid_low AND cid_high) --
    if _has_distinct_pairs(conn):
        pairs = conn.execute("""
            SELECT cid_low, cid_high, note
              FROM distinct_pairs
             WHERE cid_low = ? OR cid_high = ?
             ORDER BY cid_low, cid_high
        """, (cid, cid)).fetchall()
        print(f"    distinct_pairs ({len(pairs)}):")
        for p in pairs:
            note = f"  note: {p['note']}" if p["note"] else ""
            print(f"      ({p['cid_low']}, {p['cid_high']}){note}")

    return True


def collect_counts(conn, ids):
    """Return total rows present across the target ids, per table.

    Called before deletion so the counts can be reported as rows deleted.
    """
    ph = _placeholders(ids)

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
    # {ph} appears twice in the query -> pass ids + ids.
    rels = conn.execute(
        f"SELECT COUNT(*) FROM relationships "
        f"WHERE character_a IN ({ph}) OR character_b IN ({ph})",
        ids + ids,
    ).fetchone()[0]

    facs = conn.execute(
        f"SELECT COUNT(*) FROM character_factions WHERE character_id IN ({ph})",
        ids,
    ).fetchone()[0]

    # distinct_pairs: target may sit on either FK column (cid_low / cid_high).
    # {ph} appears twice -> pass ids + ids.
    if _has_distinct_pairs(conn):
        dpairs = conn.execute(
            f"SELECT COUNT(*) FROM distinct_pairs "
            f"WHERE cid_low IN ({ph}) OR cid_high IN ({ph})",
            ids + ids,
        ).fetchone()[0]
    else:
        dpairs = 0

    return chars, aliases, apps, rels, facs, dpairs


# ── Deletion (called only after explicit confirmation) ────────────────────────

def delete_all(conn, ids):
    """Delete all target ids and their dependents in foreign-key-safe order.

    Caller is responsible for the surrounding transaction (begin / commit /
    rollback).  Order: join tables and dependents first, characters row last.
    This satisfies all FK constraints with PRAGMA foreign_keys = ON.
    """
    ph = _placeholders(ids)

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

    # 5. distinct_pairs (cid_low and cid_high both FK -> characters; no inbound
    #    FKs, so its order vs the other child tables is irrelevant, but it must
    #    precede the characters root delete below). This is the FK that caused a
    #    live "FOREIGN KEY constraint failed" when a targeted character had been
    #    seeded into a Check E suppression pair.
    if _has_distinct_pairs(conn):
        n_dpairs = conn.execute(
            f"DELETE FROM distinct_pairs "
            f"WHERE cid_low IN ({ph}) OR cid_high IN ({ph})",
            ids + ids,
        ).rowcount
    else:
        n_dpairs = 0

    # 6. characters (root row — deleted last)
    n_chars = conn.execute(
        f"DELETE FROM characters WHERE character_id IN ({ph})",
        ids,
    ).rowcount

    return n_chars, n_aliases, n_apps, n_rels, n_facs, n_dpairs


def verify_deleted(conn, ids):
    """Return any target ids still present in characters after deletion."""
    ph = _placeholders(ids)
    return conn.execute(
        f"SELECT character_id, primary_name FROM characters "
        f"WHERE character_id IN ({ph})",
        ids,
    ).fetchall()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Delete confirmed-bogus character rows from the WoT database.",
    )
    ap.add_argument(
        "--id", dest="ids_single", action="append", type=int, metavar="ID",
        help="A character_id to delete. Repeatable: --id 367 --id 410.",
    )
    ap.add_argument(
        "--ids", dest="ids_multi", type=int, nargs="+", metavar="ID",
        help="One or more character_ids: --ids 367 410.",
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
        ap.error("provide at least one character_id via --id or --ids")

    # ── Resolve DB / BAK paths ────────────────────────────────────────────────
    global DB_PATH, BAK_PATH
    db = args.db or discover_latest_db()
    if db is None:
        sys.exit("No database given and no db/wot_book*.db snapshot found.\n"
                 "Pass --db PATH to target a specific database file.")
    DB_PATH = db
    BAK_PATH = db + ".pre-deletions.bak"

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print()
    print(_SEP2)
    print("  WoT CHARACTER DIRECTORY -- DELETE CHARACTERS")
    print(f"  Mode   : {mode}")
    print(f"  DB     : {pathlib.Path(DB_PATH).resolve()}")
    print(f"  {len(target_ids)} character_id(s) targeted for deletion")
    print(_SEP2)
    print()

    # ── Open DB read-write (PRAGMA foreign_keys already set by open_db) ───────
    conn = open_db()

    # ── Step 1: print dossier for every target id ─────────────────────────────
    _header("DOSSIER: ROWS TO BE DELETED ")
    print("Every row listed below, across all five tables, will be")
    print("permanently removed from the database on commit.")

    not_found = []
    for cid in target_ids:
        if not print_dossier(conn, cid):
            not_found.append(cid)

    print()

    # ── Step 2: summary totals ────────────────────────────────────────────────
    chars_count, aliases_count, apps_count, rels_count, facs_count, \
        dpairs_count = collect_counts(conn, target_ids)

    _header("SUMMARY: WHAT WILL BE DELETED ")
    print(f"  character rows      : {chars_count}")
    print(f"  aliases rows        : {aliases_count}")
    print(f"  appearances rows    : {apps_count}")
    print(f"  relationships rows  : {rels_count}")
    print(f"  character_factions  : {facs_count}")
    print(f"  distinct_pairs      : {dpairs_count}")
    if not_found:
        print()
        print(f"  Note: {len(not_found)} target id(s) not found in this "
              f"database (skipped): {not_found}")
    print()

    if chars_count == 0:
        print("Nothing to delete — all target character_ids are already absent.")
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
            print(f"  character_id={row['character_id']}"
                  f"  \"{row['primary_name']}\"")
        print("Investigate before proceeding.")
    else:
        print("Verified: all target character_ids are gone from the database.")

    # ── Final row-count report (actual rows deleted) ──────────────────────────
    del_chars, del_aliases, del_apps, del_rels, del_facs, del_dpairs = deleted
    print()
    _header("ROWS DELETED (actual) ")
    print(f"  characters          : {del_chars}")
    print(f"  aliases             : {del_aliases}")
    print(f"  appearances         : {del_apps}")
    print(f"  relationships       : {del_rels}")
    print(f"  character_factions  : {del_facs}")
    print(f"  distinct_pairs      : {del_dpairs}")
    # Cross-check against the pre-deletion dossier counts.
    if (del_chars, del_aliases, del_apps, del_rels, del_facs, del_dpairs) != \
       (chars_count, aliases_count, apps_count, rels_count, facs_count,
        dpairs_count):
        print()
        print("  NOTE: actual deletions differ from the pre-deletion summary.")
        print(f"  pre-deletion summary was: characters={chars_count}, "
              f"aliases={aliases_count}, appearances={apps_count}, "
              f"relationships={rels_count}, character_factions={facs_count}, "
              f"distinct_pairs={dpairs_count}")
        print("  A relationship or distinct_pair between two targeted characters")
        print("  is counted once on deletion but can inflate the pre-deletion")
        print("  summary.  This is expected and not an error.")

    conn.close()


if __name__ == "__main__":
    main()
