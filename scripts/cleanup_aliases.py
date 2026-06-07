#!/usr/bin/env python3
"""
cleanup_aliases.py - retire redundant/junk aliases and promote primary_name to
the fullest known proper name, on a single book snapshot.

DRY-RUN by default: prints the full plan (promotions + per-alias DROP / COLLAPSE
/ KEEP / REVIEW) and writes nothing. Pass --commit to apply it (a .bak is taken
first). Default --db is db/wot_book9.db; this is intended for book 9 only for
now.

Policy (shared with reconcile.py / directory_rules.py — no logic is duplicated):
  * promote primary_name to the fullest proper name among the character's own
    names (extend, never replace — superset guard) via apply_promotion.
  * PROTECTED alias types (primary, given_name, disguise) are never dropped.
  * DROPPABLE alias types (title, epithet, nickname) are AUTO-DROPPED only when
    high-confidence junk:
      - rank/article-decorated redundancy of the name ('Lord Agelmar',
        'Verin Sedai', 'Mistress Mathwin', 'High Lady Alteima',
        'Agelmar Dai Shan'),
      - descriptor-plus-own-name epithet ('stout little Verin'),
      - a corrected misnomer (its note says it was a mistake).
  * an article-only duplicate of a title/epithet COLLAPSES to the 'the' form
    (keep 'the Dark One', drop 'dark one').
  * ambiguous cases (generic-role epithet like 'Brown sister' / 'the gleeman',
    or odd residue) are flagged REVIEW and NEVER auto-deleted.
  * display_name is retired: any differing value is preserved as a given_name
    alias, then the column is dropped (book 9 only).

Every decision is written to data/alias_cleanup_<dbstem>.csv for traceability.

Usage:
    python scripts/cleanup_aliases.py                 # dry-run on db/wot_book9.db
    python scripts/cleanup_aliases.py --commit        # apply
    python scripts/cleanup_aliases.py --db db/wot_book9.db --commit
"""
import argparse
import csv
import os
import shutil
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

from directory_rules import (                                   # noqa: E402
    norm, strip_titles, is_rank_decorated_redundant,
    is_descriptor_epithet, is_corrected_misnomer, ROLE_NOUN_EXACT,
)
from reconcile import (                                         # noqa: E402
    promotion_candidates, choose_canonical_name, apply_promotion,
    name_token_set, _has_column,
)

DEFAULT_DB = os.path.join(HERE, "..", "db", "wot_book9.db")
DATA_DIR = os.path.join(HERE, "..", "data")

# Ajah-sibling role words that mark a generic-role epithet ('Brown sister').
_AJAH_ROLE_WORDS = {"sister", "brother"}


def is_generic_role_epithet(anorm):
    """True if an alias is a generic role/title rather than an identifying name
    ('the gleeman', 'the Mayor', 'Brown sister'). These are surfaced for REVIEW,
    never auto-deleted. Positional titles ('Lord of Fal Dara') are spared."""
    bare = anorm[4:] if anorm.startswith("the ") else anorm
    if bare in ROLE_NOUN_EXACT:
        return True
    return bool(set(anorm.split()) & _AJAH_ROLE_WORDS)


def classify_alias(anorm, atype, notes, nts, sibling_norms):
    """Return (action, rule) for one DROPPABLE alias.

    action in DROP / COLLAPSE / REVIEW / KEEP. Auto-applied: DROP, COLLAPSE.
    """
    if is_corrected_misnomer(notes):
        return "DROP", "corrected_misnomer"
    if is_rank_decorated_redundant(anorm, nts):
        return "DROP", "rank_decorated"
    if is_descriptor_epithet(anorm, nts):
        return "DROP", "descriptor_epithet"
    if atype in ("title", "epithet") and not anorm.startswith("the ") \
            and ("the " + anorm) in sibling_norms:
        return "COLLAPSE", "article_dup"            # drop bare, keep 'the' form
    if is_generic_role_epithet(anorm):
        return "REVIEW", "generic_role"
    if not strip_titles(anorm):
        return "REVIEW", "empty_residue"
    return "KEEP", ""


def _planned_identity_tokens(conn, cid, planned_primary, current_primary):
    """Identity token set after the (planned) promotion: primary + given_name +
    legacy display_name cores. Mirrors reconcile.name_token_set on the
    post-promotion state, for dry-run planning."""
    toks = set(strip_titles(norm(planned_primary)).split())
    toks |= set(strip_titles(norm(current_primary)).split())
    for (t,) in conn.execute(
        "SELECT alias_text FROM aliases WHERE character_id = ? "
        "AND alias_type = 'given_name'", (cid,)
    ):
        toks |= set(strip_titles(norm(t)).split())
    if _has_column(conn, "characters", "display_name"):
        row = conn.execute(
            "SELECT display_name FROM characters WHERE character_id = ?", (cid,)
        ).fetchone()
        dn = (row[0] or "").strip() if row else ""
        if dn:
            toks |= set(strip_titles(norm(dn)).split())
    return toks


def _droppable_aliases(conn, cid):
    return conn.execute(
        "SELECT alias_text, alias_norm, alias_type, notes FROM aliases "
        "WHERE character_id = ? AND alias_type IN "
        "('title', 'epithet', 'nickname') ORDER BY alias_type, alias_norm",
        (cid,),
    ).fetchall()


def _sibling_norms(conn, cid):
    return {r[0] for r in conn.execute(
        "SELECT alias_norm FROM aliases WHERE character_id = ? "
        "AND alias_type IN ('title', 'epithet')", (cid,))}


def backfill_display_names(conn):
    """Preserve every differing display_name as a given_name alias before the
    column is dropped, so apply_promotion can adopt it. Returns the count."""
    if not _has_column(conn, "characters", "display_name"):
        return 0
    n = 0
    for cid, primary, dn in conn.execute(
        "SELECT character_id, primary_name, display_name FROM characters "
        "WHERE display_name IS NOT NULL AND TRIM(display_name) <> ''"
    ).fetchall():
        if norm(dn) == norm(primary):
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO aliases "
            "(character_id, alias_text, alias_norm, alias_type, is_primary) "
            "VALUES (?, ?, ?, 'given_name', 0)",
            (cid, dn.strip(), norm(dn)),
        )
        n += cur.rowcount
    return n


def assert_invariants(conn):
    """Exactly one is_primary=1 per character, and primary_name globally unique."""
    bad = conn.execute(
        "SELECT character_id, COUNT(*) c FROM aliases WHERE is_primary = 1 "
        "GROUP BY character_id HAVING c <> 1"
    ).fetchall()
    assert not bad, f"characters without exactly one primary alias: {bad}"
    dup = conn.execute(
        "SELECT primary_name, COUNT(*) c FROM characters "
        "GROUP BY primary_name HAVING c > 1"
    ).fetchall()
    assert not dup, f"duplicate primary_name(s): {dup}"


def main():
    ap = argparse.ArgumentParser(
        description="Retire redundant aliases + promote primary_name "
                    "(dry-run by default).")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help="snapshot to clean (default: db/wot_book9.db)")
    ap.add_argument("--commit", action="store_true",
                    help="apply the plan (a .bak is written first). Without it, "
                         "this is a dry-run that writes nothing.")
    ap.add_argument("--no-export", action="store_true",
                    help="skip the public-DB re-export after --commit (for "
                         "testing against a throwaway copy).")
    args = ap.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.exists(db_path):
        sys.exit(f"Database not found: {db_path}")
    mode = "COMMIT" if args.commit else "DRY-RUN"

    if args.commit:
        bak = db_path + ".pre-alias-cleanup.bak"
        shutil.copy2(db_path, bak)
        for ext in ("-wal", "-shm"):
            side = db_path + ext
            if os.path.exists(side):
                shutil.copy2(side, bak + ext)
        print(f"Backup written: {bak}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    print(f"\n{'='*64}\n  ALIAS CLEANUP  --  {mode}\n  DB: {db_path}\n{'='*64}\n")

    # display_name retirement: preserve differing values as given_name aliases.
    if args.commit:
        moved = backfill_display_names(conn)
        if moved:
            print(f"display_name retirement: preserved {moved} value(s) as "
                  f"given_name aliases.")

    cids = [r[0] for r in conn.execute(
        "SELECT character_id FROM characters ORDER BY character_id")]

    rows = []              # CSV rows (cid, primary, alias_text, alias_type, action, rule, note)
    reviews = []           # (cid, primary, alias_text, rule, note)
    counts = {"PROMOTE": 0, "PROMOTE_BLOCKED": 0,
              "DROP": 0, "COLLAPSE": 0, "REVIEW": 0, "KEEP": 0}

    for cid in cids:
        cands = promotion_candidates(conn, cid)
        if not cands:
            continue
        current_primary = cands[0][0]
        chosen = choose_canonical_name(cands)
        planned_primary = current_primary

        # --- promotion decision ---
        if chosen and norm(chosen) != norm(current_primary):
            dup = conn.execute(
                "SELECT character_id FROM characters "
                "WHERE primary_name = ? AND character_id != ?", (chosen, cid)
            ).fetchone()
            if dup:
                counts["PROMOTE_BLOCKED"] += 1
                note = (f"name already used by character_id {dup[0]}; "
                        f"queued for review, not promoted")
                rows.append((cid, current_primary, chosen, "primary",
                             "PROMOTE_BLOCKED", "promotion", note))
                reviews.append((cid, current_primary, chosen,
                                "promotion_blocked_duplicate", note))
            else:
                planned_primary = chosen
                counts["PROMOTE"] += 1
                rows.append((cid, planned_primary, chosen, "primary",
                             "PROMOTE", "promotion",
                             f"from '{current_primary}'"))

        # --- commit promotion, then classify against the REAL state ---
        if args.commit and planned_primary != current_primary:
            apply_promotion(conn, cid)
            nts = name_token_set(conn, cid)
        else:
            nts = _planned_identity_tokens(
                conn, cid, planned_primary, current_primary)

        siblings = _sibling_norms(conn, cid)
        for alias_text, anorm, atype, notes in _droppable_aliases(conn, cid):
            action, rule = classify_alias(anorm, atype, notes, nts, siblings)
            counts[action] += 1
            rows.append((cid, planned_primary, alias_text, atype, action,
                         rule, notes or ""))
            if action == "REVIEW":
                reviews.append((cid, planned_primary, alias_text, rule, notes))
            if args.commit and action in ("DROP", "COLLAPSE"):
                conn.execute(
                    "DELETE FROM aliases WHERE character_id = ? "
                    "AND alias_norm = ?", (cid, anorm))

    # drop the legacy display_name column (book 9) after promotions used it.
    dropped_col = False
    if args.commit and _has_column(conn, "characters", "display_name"):
        conn.execute("ALTER TABLE characters DROP COLUMN display_name")
        dropped_col = True

    assert_invariants(conn)

    if args.commit:
        conn.commit()
    conn.close()

    # --- CSV trace ---
    os.makedirs(DATA_DIR, exist_ok=True)
    stem = os.path.splitext(os.path.basename(db_path))[0]
    csv_path = os.path.join(DATA_DIR, f"alias_cleanup_{stem}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["character_id", "primary_name", "alias_text",
                    "alias_type", "action", "rule", "note"])
        w.writerows(rows)

    # --- summary ---
    print("Plan summary" + (" (APPLIED)" if args.commit else " (dry-run)") + ":")
    print(f"  promotions:           {counts['PROMOTE']}")
    print(f"  promotions blocked:   {counts['PROMOTE_BLOCKED']}")
    print(f"  aliases DROP:         {counts['DROP']}")
    print(f"  aliases COLLAPSE:     {counts['COLLAPSE']}")
    print(f"  aliases REVIEW:       {counts['REVIEW']}  (never auto-deleted)")
    print(f"  aliases KEEP:         {counts['KEEP']}")
    if dropped_col:
        print("  display_name column:  DROPPED")
    elif not args.commit and _column_present(db_path):
        print("  display_name column:  would be DROPPED on --commit")
    print(f"  trace written:        {csv_path}")

    if reviews:
        print(f"\nREVIEW — {len(reviews)} item(s) for a human (NOT deleted):")
        for cid, primary, alias_text, rule, note in reviews:
            extra = f"  note: {note}" if note else ""
            print(f"  cid={cid} {primary!r}: {alias_text!r} [{rule}]{extra}")
    else:
        print("\nREVIEW: none.")

    if not args.commit:
        print("\nDry-run only — nothing was written. Re-run with --commit to "
              "apply, then: python scripts/export_public_dbs.py")
    elif args.no_export:
        print("\nApplied. (--no-export: skipped public-DB re-export.)")
    else:
        print("\nApplied. Re-exporting public snapshots...")
        try:
            subprocess.run(
                [sys.executable, os.path.join(HERE, "export_public_dbs.py")],
                check=True)
        except Exception as exc:
            print(f"  (export failed: {exc} — run "
                  f"'python scripts/export_public_dbs.py' manually)")


def _column_present(db_path):
    c = sqlite3.connect(db_path)
    try:
        return any(r[1] == "display_name"
                   for r in c.execute("PRAGMA table_info(characters)"))
    finally:
        c.close()


if __name__ == "__main__":
    main()
