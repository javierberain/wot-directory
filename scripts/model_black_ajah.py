#!/usr/bin/env python3
"""
model_black_ajah.py - Model the Black Ajah as a covert OVERLAPPING faction.

The Black Ajah is additive: a Black sister keeps her public Ajah AND gains a
separate `Black Ajah` membership. Books were originally extracted with the Black
Ajah recorded only as a note ("Black Ajah", "formerly Green Ajah") while the
sister sat in a generic `The Shadow` faction. This tool fixes that for a given
snapshot:

  1. Detect Black Ajah members: a character whose **The Shadow / White Tower**
     membership note says "Black Ajah" (their own allegiance). Excludes
     "working with the Black Ajah" phrasing — that flags allies (e.g. a Forsaken
     coordinating with them), not members.
  2. Create the `Black Ajah` faction (faction_type 'ajah') and add each member
     to it (additive).
  3. Derive the public Ajah from "former(ly) X Ajah" notes ONLY where locally
     stated; never invented. Members with an explicit public-Ajah faction
     already (e.g. Liandrin / Red Ajah) keep it.
  4. Remove the member's `The Shadow` membership (it existed only because she
     is Black Ajah; the Black Ajah faction now conveys that).
  5. Clean redundant/misleading White Tower notes ("Black Ajah member",
     "Former X Ajah") to NULL.

Forsaken, named Darkfriends, and Shadowspawn keep their independent `The Shadow`
membership untouched.

Dry-run by default; --commit writes after a backup; FK-checked.

    python scripts/model_black_ajah.py --db db/wot_book3.db
    python scripts/model_black_ajah.py --db db/wot_book3.db --commit
"""
import argparse
import os
import pathlib
import re
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
from directory_rules import norm  # noqa: E402

_FORMER_AJAH = re.compile(r"former(?:ly)?\s+([A-Za-z']+)\s+ajah", re.IGNORECASE)


def take_backup(db_path):
    p = pathlib.Path(db_path).resolve()
    bak = pathlib.Path(str(p) + ".pre-black-ajah.bak")
    shutil.copy2(p, bak)
    for ext in ("-wal", "-shm"):
        s = pathlib.Path(str(p) + ext)
        if s.exists():
            shutil.copy2(s, pathlib.Path(str(bak) + ext))
    print(f"Backup written: {bak}")


def get_or_create_faction(cur, name, ftype="ajah"):
    nn = norm(name)
    r = cur.execute("SELECT faction_id FROM factions WHERE name_norm = ?",
                    (nn,)).fetchone()
    if r:
        return r[0]
    cur.execute("INSERT INTO factions (name, name_norm, faction_type) "
                "VALUES (?, ?, ?)", (name, nn, ftype))
    return cur.lastrowid


def run(db_path, commit):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    # 1. detect members (own Black Ajah allegiance on Shadow/White Tower)
    members = cur.execute("""
        SELECT DISTINCT ch.character_id cid, ch.primary_name pn
          FROM characters ch
          JOIN character_factions cf ON cf.character_id = ch.character_id
          JOIN factions f ON f.faction_id = cf.faction_id
         WHERE f.name_norm IN ('the shadow', 'white tower')
           AND cf.notes LIKE '%Black Ajah%'
           AND cf.notes NOT LIKE '%working with%'
         ORDER BY ch.primary_name
    """).fetchall()

    print(f"\n{'DRY RUN' if not commit else 'COMMIT'}: {db_path}")
    print(f"{len(members)} Black Ajah member(s) detected:\n")

    plan = []
    for m in members:
        cid = m["cid"]
        facs = cur.execute(
            "SELECT f.name, f.name_norm, cf.notes FROM character_factions cf "
            "JOIN factions f ON f.faction_id = cf.faction_id "
            "WHERE cf.character_id = ?", (cid,)).fetchall()
        has_ajah = any(f["name_norm"].endswith(" ajah") and
                       f["name_norm"] != "black ajah" for f in facs)
        in_shadow = any(f["name_norm"] == "the shadow" for f in facs)
        # derive public Ajah from notes/description if not already present
        derived = None
        if not has_ajah:
            blob = " ".join((f["notes"] or "") for f in facs)
            desc = cur.execute("SELECT description FROM characters WHERE "
                               "character_id = ?", (cid,)).fetchone()[0] or ""
            mo = _FORMER_AJAH.search(blob + " " + desc)
            if mo:
                derived = mo.group(1).capitalize() + " Ajah"
        plan.append((cid, m["pn"], has_ajah, in_shadow, derived))
        pub = ("keeps public Ajah" if has_ajah else
               (f"+{derived}" if derived else "public Ajah unknown (local)"))
        print(f"  cid={cid:<4} {m['pn']:<22} +Black Ajah; {pub}"
              + ("; remove The Shadow" if in_shadow else ""))

    if not commit:
        print("\nDry-run complete. Re-run with --commit to apply.")
        conn.close()
        return

    take_backup(db_path)
    try:
        ba = get_or_create_faction(cur, "Black Ajah", "ajah")
        shadow = cur.execute("SELECT faction_id FROM factions WHERE "
                             "name_norm = 'the shadow'").fetchone()
        shadow = shadow[0] if shadow else None
        for cid, pn, has_ajah, in_shadow, derived in plan:
            cur.execute("INSERT OR IGNORE INTO character_factions "
                        "(character_id, faction_id, role, notes) "
                        "VALUES (?, ?, 'member', 'Sworn to the Shadow (covert).')",
                        (cid, ba))
            if derived:
                pf = get_or_create_faction(cur, derived, "ajah")
                cur.execute("INSERT OR IGNORE INTO character_factions "
                            "(character_id, faction_id, role, notes) VALUES "
                            "(?, ?, 'member', 'Public Ajah (also Black Ajah).')",
                            (cid, pf))
            if shadow:
                cur.execute("DELETE FROM character_factions WHERE "
                            "character_id = ? AND faction_id = ?", (cid, shadow))
            # clean redundant White Tower notes
            cur.execute("""UPDATE character_factions SET notes = NULL
                WHERE character_id = ?
                  AND faction_id = (SELECT faction_id FROM factions
                                    WHERE name_norm = 'white tower')
                  AND (notes LIKE '%Black Ajah%' OR notes LIKE '%Former%Ajah%'
                       OR notes LIKE '%formerly%')""", (cid,))
        viol = conn.execute("PRAGMA foreign_key_check").fetchall()
        if viol:
            raise RuntimeError(f"FK violations: {viol}")
        conn.commit()
        print(f"\nCOMMITTED. Black Ajah faction id={ba}; {len(plan)} member(s) "
              f"made additive and removed from The Shadow.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    run(args.db, args.commit)


if __name__ == "__main__":
    main()
