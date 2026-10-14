#!/usr/bin/env python3
"""
revert_prequel_fills.py - Undo time-dependent facts a PREQUEL snapshot wrote
onto characters that already existed in the snapshot it was seeded from.

New Spring (slot newspring) was seeded from book10. Its chapters are set twenty
years before book 1, so a trait filled from them on a pre-existing character
('age: about twenty' on Moiraine) or a faction attached to one ('Accepted') is a
978 NE fact presented as current. reconcile.py now refuses such writes when
books.is_prequel is set; this script repairs a snapshot ingested before that.

It diffs the prequel snapshot against its seed for every character that exists
in BOTH:
  - characters.physical_traits / age / filiations / personality: a value that
    was NULL/empty in the seed and is filled in the prequel -> REVERT to NULL
  - character_factions: a (character, faction) join present in the prequel but
    not in the seed -> REMOVE (the faction row itself is kept)
Nationality, names, aliases, appearances, mentions and relationships are not
touched (they are timeless or correctly chapter-dated).

Dry-run by default; --commit writes after a .bak.

    python scripts/revert_prequel_fills.py --prequel newspring --seed book10
    python scripts/revert_prequel_fills.py --prequel newspring --seed book10 --commit
"""
import argparse, os, shutil, sqlite3, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from directory_rules import snapshot_path  # noqa: E402

TRAITS = ("physical_traits", "age", "personality")      # traits that expire
GUARDED_FACTION_TYPES = ("other", "order")              # factions one can leave


def empty(v):
    return v is None or not str(v).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prequel", required=True, help="slot, e.g. newspring")
    ap.add_argument("--seed", required=True, help="slot it was seeded from, e.g. book10")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    pre_path, seed_path = snapshot_path(a.prequel), snapshot_path(a.seed)
    for p in (pre_path, seed_path):
        if not os.path.exists(p):
            sys.exit(f"not found: {p}")
    if a.commit:
        bak = pre_path + ".pre-prequel-revert.bak"
        shutil.copy2(pre_path, bak)
        print(f"Backup written: {bak}")
    conn = sqlite3.connect(pre_path)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS seed", (seed_path,))

    # existing characters = present in both snapshots AND on the page in the
    # main series (a character the main series only names takes her only
    # description from the prequel, so she is left alone)
    main_series = {r[0] for r in conn.execute("""
        SELECT DISTINCT a.character_id FROM appearances a
          JOIN chapters ch ON ch.chapter_id = a.chapter_id
          JOIN books b ON b.book_id = ch.book_id WHERE b.is_prequel = 0""")}
    trait_fixes = []
    for r in conn.execute(f"""
        SELECT p.character_id, p.primary_name,
               {", ".join(f"p.{t} AS p_{t}, s.{t} AS s_{t}" for t in TRAITS)}
          FROM characters p JOIN seed.characters s ON s.character_id = p.character_id
    """):
        if r["character_id"] not in main_series:
            continue
        for t in TRAITS:
            if empty(r[f"s_{t}"]) and not empty(r[f"p_{t}"]):
                trait_fixes.append((r["character_id"], r["primary_name"], t, r[f"p_{t}"]))

    fac_fixes = [r for r in conn.execute("""
        SELECT cf.character_id, c.primary_name, cf.faction_id, f.name, f.faction_type, cf.role
          FROM character_factions cf
          JOIN characters c ON c.character_id = cf.character_id
          JOIN factions f ON f.faction_id = cf.faction_id
         WHERE cf.character_id IN (SELECT character_id FROM seed.characters)
           AND NOT EXISTS (SELECT 1 FROM seed.character_factions sf
                            WHERE sf.character_id = cf.character_id
                              AND sf.faction_id = cf.faction_id)
    """) if r["character_id"] in main_series
         and (r["faction_type"] or "other") in GUARDED_FACTION_TYPES]

    print(f"{'COMMIT' if a.commit else 'DRY RUN'}: {a.prequel} vs seed {a.seed}")
    print(f"\nTrait fills on pre-existing characters: {len(trait_fixes)}")
    for cid, name, t, v in trait_fixes:
        print(f"  cid={cid} {name!r} {t}: {str(v)[:90]!r}  -> NULL")
    print(f"\nLeavable faction joins added to main-series characters: {len(fac_fixes)}")
    for r in fac_fixes:
        print(f"  cid={r['character_id']} {r['primary_name']!r} -> {r['name']!r} [{r['faction_type']}] ({r['role']})  -> REMOVE")

    if a.commit:
        for cid, _, t, _ in trait_fixes:
            conn.execute(f"UPDATE characters SET {t} = NULL, updated_at = datetime('now') "
                         f"WHERE character_id = ?", (cid,))
        for r in fac_fixes:
            conn.execute("DELETE FROM character_factions WHERE character_id = ? AND faction_id = ?",
                         (r["character_id"], r["faction_id"]))
        conn.commit()
        print(f"\nCOMMITTED: {len(trait_fixes)} trait(s) reverted, {len(fac_fixes)} faction join(s) removed.")
    else:
        print("\nDry run only. Re-run with --commit to apply.")
    conn.close()


if __name__ == "__main__":
    main()
