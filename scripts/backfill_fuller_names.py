#!/usr/bin/env python3
"""
backfill_fuller_names.py - Promote primary names to the fullest proper form the
chapter text actually used.

reconcile.update_existing used to record only `aliases_observed` for an existing
character, never the chapter's own `name_used_in_text`, so a row first seen as
'Ituralde' stayed 'Ituralde' even after a chapter called him 'Rodel Ituralde'.
That gap is now closed in reconcile.py; this script repairs rows ingested
before the fix, using the evidence already in the snapshot: appearances.name_used.

For each character, every distinct appearances.name_used that
directory_rules.is_fuller_proper_name() judges a strict proper-name extension of
the current primary is added as a given_name alias (note: "fuller form used in
chapter text (backfill)"), then reconcile.apply_promotion() runs, so the same
promotion rules as live ingestion decide the new primary (and a clash with an
existing primary_name is queued for review, not forced).

Dry-run by default; --commit writes after taking a .bak.

    python scripts/backfill_fuller_names.py --db db/wot_book10.db
    python scripts/backfill_fuller_names.py --db db/wot_book10.db --commit
"""
import argparse, os, shutil, sqlite3, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from directory_rules import is_fuller_proper_name, norm  # noqa: E402
import reconcile  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not os.path.exists(a.db):
        sys.exit(f"not found: {a.db}")
    if a.commit:
        bak = a.db + ".pre-fuller-names.bak"
        shutil.copy2(a.db, bak)
        print(f"Backup written: {bak}")
    conn = sqlite3.connect(a.db)
    conn.execute("PRAGMA foreign_keys = ON")
    rows = conn.execute("""
        SELECT c.character_id, c.primary_name, a.name_used, COUNT(*) AS n
          FROM appearances a JOIN characters c ON c.character_id = a.character_id
         WHERE a.name_used IS NOT NULL
           AND lower(trim(a.name_used)) <> lower(trim(c.primary_name))
         GROUP BY c.character_id, a.name_used
         ORDER BY c.character_id""").fetchall()
    todo = {}
    for cid, prim, used, n in rows:
        if is_fuller_proper_name(used, prim):
            todo.setdefault(cid, (prim, []))[1].append((used.strip(), n))
    print(f"{'COMMIT' if a.commit else 'DRY RUN'}: {len(todo)} character(s) with a fuller name in chapter text\n")
    promoted = 0
    for cid, (prim, useds) in todo.items():
        print(f"  cid={cid} '{prim}'  <- " + ", ".join(f"'{u}' x{n}" for u, n in useds))
        if not a.commit:
            continue
        for used, _ in useds:
            conn.execute(
                "INSERT OR IGNORE INTO aliases (character_id, alias_text, alias_norm, "
                "alias_type, is_primary, notes) VALUES (?, ?, ?, 'given_name', 0, ?)",
                (cid, used, norm(used), "fuller form used in chapter text (backfill)"))
        new = reconcile.apply_promotion(conn, cid)
        if new:
            promoted += 1
            print(f"      -> promoted to '{new}'")
        else:
            print(f"      -> not promoted (see review_queue if a duplicate blocked it)")
    if a.commit:
        conn.commit()
        print(f"\nCOMMITTED. {promoted} primary name(s) promoted.")
    else:
        print("\nDry run only. Re-run with --commit to apply.")
    conn.close()


if __name__ == "__main__":
    main()
