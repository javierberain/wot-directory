#!/usr/bin/env python3
"""
backfill_appearances.py - Recover dropped appearance/relationship rows in an
already-cleaned snapshot by replaying the saved extraction JSON, ADDITIVELY.

The Phase 0 mention audit showed hundreds of characters who are clearly present
in a chapter (named many times) but have no appearances row — the silent
drop-on-unresolved bug in the old reconcile.py. This tool recovers them WITHOUT
re-running the destructive parts of reconciliation:

  * It resolves each extracted name ONLY against rows that ALREADY EXIST in the
    snapshot (reconcile.Roster: exact / token-subset / verified LLM pointer).
    Ambiguous or unknown names are skipped and logged.
  * It NEVER creates a character and NEVER merges — so deliberately-deleted
    placeholders stay deleted and manual merges are preserved.
  * It only INSERTs MISSING appearance rows (INSERT OR IGNORE on the
    (character_id, chapter_id) unique key), so existing, possibly hand-curated
    rows are untouched. Same for relationships.
  * Chapters are matched by (series_order, chapter_number) in the TARGET db, not
    by the extraction's stored chapter_id, so it is robust across snapshots.

For snapshot wot_bookN.db it replays books 1..N (the books that snapshot
contains). Dry-run by default; --commit writes after a backup.

    python scripts/backfill_appearances.py --db db/wot_book3.db --max-book 3
    python scripts/backfill_appearances.py --db db/wot_book3.db --max-book 3 --commit
"""
import argparse
import glob
import json
import os
import pathlib
import re
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
from reconcile import Roster, norm  # noqa: E402

HERE = os.path.dirname(__file__)
EXTRACT_DIR = os.path.join(HERE, "..", "data", "extractions")


def take_backup(db_path):
    p = pathlib.Path(db_path).resolve()
    bak = pathlib.Path(str(p) + ".pre-backfill.bak")
    shutil.copy2(p, bak)
    for ext in ("-wal", "-shm"):
        s = pathlib.Path(str(p) + ext)
        if s.exists():
            shutil.copy2(s, pathlib.Path(str(bak) + ext))
    print(f"Backup written: {bak}")


def chapter_id_for(conn, series_order, chapter_number):
    r = conn.execute(
        "SELECT ch.chapter_id FROM chapters ch JOIN books b "
        "ON b.book_id = ch.book_id "
        "WHERE b.series_order = ? AND ch.chapter_number = ?",
        (series_order, chapter_number)).fetchone()
    return r[0] if r else None


def _extraction_files(book):
    pat = os.path.join(EXTRACT_DIR, f"b{book}_c*.json")
    def cnum(p):
        m = re.search(r"_c(\d+)\.json$", p)
        return int(m.group(1)) if m else 0
    return sorted(glob.glob(pat), key=cnum)


def resolve_existing_cid(roster, name):
    """Confident match to an existing row, or None."""
    cid, method, _ = roster.resolve_existing(name)
    return cid if method in ("exact", "token_subset", "llm_pointer") else None


def backfill(db_path, max_book, commit):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    roster = Roster(conn)   # no creates happen, so the index stays valid

    added_apps = added_rels = 0
    skipped_names = set()

    for book in range(1, max_book + 1):
        for path in _extraction_files(book):
            data = json.load(open(path, encoding="utf-8"))
            meta = data.get("_meta", {})
            chapter_id = chapter_id_for(
                conn, meta.get("book_order", book), meta.get("chapter_number"))
            if chapter_id is None:
                continue

            # name -> cid for everything resolvable in this chapter (for rels)
            local = {}
            for app in data.get("appearances", []):
                name = (app.get("character") or "").strip()
                if not name:
                    continue
                cid = local.get(norm(name))
                if cid is None:
                    cid = resolve_existing_cid(roster, name)
                    if cid is None:
                        skipped_names.add(name)
                        continue
                    local[norm(name)] = cid
                exists = conn.execute(
                    "SELECT 1 FROM appearances WHERE character_id = ? AND "
                    "chapter_id = ?", (cid, chapter_id)).fetchone()
                if exists:
                    continue
                alliances = app.get("alliances_shown") or []
                if commit:
                    conn.execute(
                        "INSERT OR IGNORE INTO appearances "
                        "(character_id, chapter_id, name_used, whereabouts, "
                        "notable_actions, alliances_shown, demeanor) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (cid, chapter_id, app.get("character"),
                         app.get("whereabouts"), app.get("notable_actions"),
                         ", ".join(alliances) if alliances else None,
                         app.get("demeanor")))
                added_apps += 1

            # relationships whose BOTH endpoints resolve to existing rows
            for rel in data.get("relationships", []):
                a = local.get(norm(rel.get("character_a") or "")) or \
                    resolve_existing_cid(roster, rel.get("character_a") or "")
                b = local.get(norm(rel.get("character_b") or "")) or \
                    resolve_existing_cid(roster, rel.get("character_b") or "")
                if a is None or b is None or a == b:
                    continue
                directed = bool(rel.get("directed"))
                x, y = (a, b) if directed else tuple(sorted((a, b)))
                rtype = rel.get("relationship_type", "other")
                exists = conn.execute(
                    "SELECT 1 FROM relationships WHERE character_a = ? AND "
                    "character_b = ? AND relationship_type = ?",
                    (x, y, rtype)).fetchone()
                if exists:
                    continue
                if commit:
                    conn.execute(
                        "INSERT OR IGNORE INTO relationships "
                        "(character_a, character_b, relationship_type, "
                        "directed, description, first_chapter_id) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (x, y, rtype, 1 if directed else 0,
                         rel.get("description"), chapter_id))
                added_rels += 1

    print(f"\n{'DRY RUN' if not commit else 'COMMIT'}: {db_path} "
          f"(books 1..{max_book})")
    print(f"  appearance rows to add:   {added_apps}")
    print(f"  relationship rows to add: {added_rels}")
    print(f"  distinct unresolved names skipped: {len(skipped_names)}")
    if skipped_names:
        sample = sorted(skipped_names)[:15]
        print("    e.g. " + "; ".join(sample))

    if commit and (added_apps or added_rels):
        conn.commit()
        print("  COMMITTED.")
    elif not commit:
        print("  Dry-run; re-run with --commit to write.")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--max-book", type=int, required=True,
                    help="highest series_order this snapshot contains")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    if args.commit:
        take_backup(args.db)
    backfill(args.db, args.max_book, args.commit)


if __name__ == "__main__":
    main()
