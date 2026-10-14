#!/usr/bin/env python3
"""
normalize_ages.py - Rewrite characters.age into the directory's age vocabulary.

The extractor returned age as free text. directory_rules.normalize_age maps it
to one of: child, young, adult, middle years, older, elderly, ageless, or
"about N" when a number was given. Non-age values ("alive", "deceased") and
pure comparatives ("older than Rand") become NULL and are listed so a human
can supply a value by hand if one is known from the text.

Dry-run by default; --commit writes after a .bak. Idempotent.

    python scripts/normalize_ages.py --db db/wot_book10.db
    python scripts/normalize_ages.py --db db/wot_book10.db --commit
"""
import argparse, collections, os, shutil, sqlite3, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from directory_rules import normalize_age  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if a.commit:
        bak = a.db + ".pre-age-normalize.bak"
        shutil.copy2(a.db, bak)
        print(f"Backup written: {bak}")
    conn = sqlite3.connect(a.db)
    rows = conn.execute("SELECT character_id, primary_name, age FROM characters "
                        "WHERE age IS NOT NULL AND trim(age) <> ''").fetchall()
    changes, dropped, same = [], [], 0
    for cid, name, raw in rows:
        new = normalize_age(raw)
        if new == raw:
            same += 1
        elif new is None:
            dropped.append((cid, name, raw))
        else:
            changes.append((cid, name, raw, new))
    print(f"{'COMMIT' if a.commit else 'DRY RUN'}: {len(rows)} filled age values; "
          f"{same} already normalised, {len(changes)} rewritten, {len(dropped)} dropped to NULL\n")
    print("Rewritten:")
    for cid, name, raw, new in changes:
        print(f"  cid={cid} {name[:26]:26} {raw[:60]!r:62} -> {new!r}")
    print("\nDropped (no age recognised; set by hand if the text gives one):")
    for cid, name, raw in dropped:
        print(f"  cid={cid} {name[:26]:26} {raw[:80]!r}")
    print("\nResulting distribution:")
    dist = collections.Counter(normalize_age(r[2]) for r in rows)
    for k, v in sorted(dist.items(), key=lambda kv: -kv[1]):
        print(f"  {str(k):14} {v}")
    if a.commit:
        for cid, _, _, new in changes:
            conn.execute("UPDATE characters SET age = ?, updated_at = datetime('now') "
                         "WHERE character_id = ?", (new, cid))
        for cid, _, _ in dropped:
            conn.execute("UPDATE characters SET age = NULL, updated_at = datetime('now') "
                         "WHERE character_id = ?", (cid,))
        conn.commit()
        print(f"\nCOMMITTED.")
    else:
        print("\nDry run only. Re-run with --commit to apply.")
    conn.close()


if __name__ == "__main__":
    main()
