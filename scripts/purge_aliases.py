#!/usr/bin/env python3
"""
purge_aliases.py - Remove generic / non-identifying aliases from a snapshot,
driven by the SHARED directory_rules.is_generic_alias predicate.

This is the retrofit counterpart to the write-time gate now in reconcile.py.
delete_aliases.py required hand-curated TARGET_ALIAS_IDS and so missed cases
(the Phase 0 mention audit found "What" on Whatley Eldin and "girl"/"the girl"
on Faile still polluting the cleaned book-3 snapshot). This tool flags every
alias the shared predicate rejects, so the auditor, the write gate, and the
cleanup all agree on what "generic" means.

Never deletes a primary alias (is_primary = 1). Aliases are a leaf table, so
deletion is a single statement. Dry-run by default; --commit writes after a
backup. Also prints "suspect" single-word aliases (advisory) for a human to
review — those are NOT auto-deleted.

    python scripts/purge_aliases.py --db db/wot_book3.db            # dry-run
    python scripts/purge_aliases.py --db db/wot_book3.db --commit
"""
import argparse
import os
import pathlib
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
from directory_rules import is_generic_alias, looks_like_suspect_alias  # noqa


def take_backup(db_path):
    p = pathlib.Path(db_path).resolve()
    bak = pathlib.Path(str(p) + ".pre-purge-aliases.bak")
    shutil.copy2(p, bak)
    for ext in ("-wal", "-shm"):
        s = pathlib.Path(str(p) + ext)
        if s.exists():
            shutil.copy2(s, pathlib.Path(str(bak) + ext))
    print(f"Backup written: {bak}")


def run(db_path, commit):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT a.alias_id, a.alias_text, a.alias_type, a.is_primary,
               c.character_id, c.primary_name
          FROM aliases a JOIN characters c ON c.character_id = a.character_id
         ORDER BY c.primary_name, a.alias_text
    """).fetchall()

    flagged, suspect = [], []
    for aid, text, atype, is_primary, cid, pname in rows:
        if is_primary:
            continue
        if is_generic_alias(text):
            flagged.append((aid, text, atype, cid, pname))
        elif looks_like_suspect_alias(text):
            suspect.append((aid, text, atype, cid, pname))

    print(f"\n{'DRY RUN' if not commit else 'COMMIT'}: {db_path}")
    print(f"{len(flagged)} generic alias(es) to delete:\n")
    for aid, text, atype, cid, pname in flagged:
        print(f"  alias_id={aid:>5}  \"{text}\" [{atype}]  -> "
              f"cid={cid} \"{pname}\"")

    if suspect:
        print(f"\n{len(suspect)} SUSPECT single-word alias(es) (NOT deleted — "
              f"review by hand):")
        for aid, text, atype, cid, pname in suspect:
            print(f"  alias_id={aid:>5}  \"{text}\" [{atype}]  -> "
                  f"cid={cid} \"{pname}\"")

    if not flagged:
        print("\nNothing to delete.")
        conn.close()
        return 0
    if not commit:
        print("\nDry-run complete. Re-run with --commit to delete the "
              f"{len(flagged)} flagged alias(es).")
        conn.close()
        return len(flagged)

    take_backup(db_path)
    try:
        conn.executemany("DELETE FROM aliases WHERE alias_id = ?",
                         [(f[0],) for f in flagged])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"\nDELETED {len(flagged)} alias(es).")
    conn.close()
    return len(flagged)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    run(args.db, args.commit)


if __name__ == "__main__":
    main()
