#!/usr/bin/env python3
"""
reconcile.py - Review an extraction JSON file and commit it to the database.

This is the careful step. The extractor produced raw JSON; this script
matches the extracted characters against the existing roster, decides
what to auto-commit and what to flag for human review, and writes the
appearances and relationships.

Matching strategy, in order:
  1. Exact match on a known alias (normalized).
  2. The LLM's own "likely_matches_existing" pointer, if it resolves.
  3. Fuzzy match (difflib) above a threshold -> flagged, not auto-merged.
  4. Genuinely new + high confidence -> create a new character.
  5. Anything ambiguous -> parked in review_queue, not committed.

Usage:
    python reconcile.py --book 1 --chapter 4            # interactive review
    python reconcile.py --book 1 --chapter 4 --auto     # commit confident items
    python reconcile.py --review                        # list the review queue
"""
import argparse
import difflib
import json
import os
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "wot.db")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "extractions")
FUZZY_THRESHOLD = 0.86   # difflib ratio above which we suggest a match
POINTER_THRESHOLD = 0.6  # minimum similarity to trust an LLM pointer (lower
                          # than FUZZY_THRESHOLD because we only need to rule
                          # out completely unrelated names, not confirm a merge)


def norm(name):
    return " ".join(name.lower().replace("’", "'").split())


def load_alias_index(conn):
    """Return {normalized_alias: character_id} for all known names."""
    idx = {}
    for cid, atext in conn.execute(
        "SELECT character_id, alias_norm FROM aliases"
    ):
        idx[atext] = cid
    return idx


def fuzzy_lookup(name, alias_index):
    """Return (character_id, score) for the best fuzzy match, or (None, 0)."""
    best_id, best_score = None, 0.0
    n = norm(name)
    for alias_norm, cid in alias_index.items():
        score = difflib.SequenceMatcher(None, n, alias_norm).ratio()
        if score > best_score:
            best_id, best_score = cid, score
    return best_id, best_score


def get_primary_name(conn, cid):
    r = conn.execute(
        "SELECT primary_name FROM characters WHERE character_id = ?", (cid,)
    ).fetchone()
    return r[0] if r else None


VALID_CHAR_TYPES = {
    "human", "ogier", "trolloc", "myrddraal", "horse", "wolf", "other",
}
VALID_FACTION_TYPES = {
    "ajah", "order", "house", "clan", "society", "other",
}


def coerce_char_type(value):
    """Sanitize the LLM's character_type. Defaults to 'human'."""
    v = (value or "human").strip().lower()
    return v if v in VALID_CHAR_TYPES else "other"


def coerce_faction_type(value):
    v = (value or "other").strip().lower()
    return v if v in VALID_FACTION_TYPES else "other"


def create_character(conn, char):
    """Insert a new character + its aliases. Returns the new character_id."""
    st = char.get("stable_traits", {}) or {}
    primary = char.get("name_used_in_text", "").strip()
    ctype = coerce_char_type(char.get("character_type"))
    cur = conn.execute(
        """INSERT INTO characters
           (primary_name, character_type, nationality, physical_traits, age,
            filiations, personality)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (primary, ctype, st.get("nationality"), st.get("physical_traits"),
         st.get("age"), st.get("filiations"), st.get("personality")),
    )
    cid = cur.lastrowid
    # Primary name as its own alias row.
    conn.execute(
        "INSERT OR IGNORE INTO aliases "
        "(character_id, alias_text, alias_norm, alias_type, is_primary) "
        "VALUES (?, ?, ?, 'primary', 1)",
        (cid, primary, norm(primary)),
    )
    add_aliases(conn, cid, char.get("aliases_observed", []))
    return cid


def add_aliases(conn, cid, aliases):
    for a in aliases or []:
        text = (a.get("alias_text") or "").strip()
        if not text:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO aliases "
            "(character_id, alias_text, alias_norm, alias_type, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (cid, text, norm(text),
             a.get("alias_type", "nickname"), a.get("notes")),
        )


def enrich_character(conn, cid, char):
    """Fill in any stable trait that is currently empty."""
    st = char.get("stable_traits", {}) or {}
    cols = {
        "nationality": st.get("nationality"),
        "physical_traits": st.get("physical_traits"),
        "age": st.get("age"),
        "filiations": st.get("filiations"),
        "personality": st.get("personality"),
    }
    for col, val in cols.items():
        if val:
            conn.execute(
                f"UPDATE characters SET {col} = COALESCE({col}, ?), "
                f"updated_at = datetime('now') WHERE character_id = ?",
                (val, cid),
            )
    add_aliases(conn, cid, char.get("aliases_observed", []))


def reconcile_factions(conn, cid, char, chapter_id):
    """Match each LLM-reported faction by name, create new ones, write join rows.

    Same generous-matching spirit as character reconciliation: lookup by
    normalized name, create on miss. Idempotent: re-running on the same
    chapter does not duplicate rows.
    """
    for fac in char.get("factions", []) or []:
        name = (fac.get("name") or "").strip()
        if not name:
            continue
        nnorm = norm(name)
        ftype = coerce_faction_type(fac.get("faction_type"))
        row = conn.execute(
            "SELECT faction_id FROM factions WHERE name_norm = ?", (nnorm,)
        ).fetchone()
        if row:
            fid = row[0]
        else:
            cur = conn.execute(
                "INSERT INTO factions (name, name_norm, faction_type) "
                "VALUES (?, ?, ?)",
                (name, nnorm, ftype),
            )
            fid = cur.lastrowid

        role = (fac.get("role") or "member").strip().lower() or "member"
        notes = fac.get("notes")
        # INSERT OR IGNORE so re-running a chapter doesn't blow up;
        # then upgrade 'member' -> 'leader' if the LLM now says leader.
        conn.execute(
            "INSERT OR IGNORE INTO character_factions "
            "(character_id, faction_id, role, first_chapter_id, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (cid, fid, role, chapter_id, notes),
        )
        if role == "leader":
            conn.execute(
                "UPDATE character_factions SET role = 'leader' "
                "WHERE character_id = ? AND faction_id = ?",
                (cid, fid),
            )


def queue_review(conn, chapter_id, kind, payload, note):
    conn.execute(
        "INSERT INTO review_queue (chapter_id, kind, payload, note) "
        "VALUES (?, ?, ?, ?)",
        (chapter_id, kind, json.dumps(payload, ensure_ascii=False), note),
    )


def reconcile(book_order, chapter_number, auto=False):
    path = os.path.join(OUT_DIR, f"b{book_order}_c{chapter_number}.json")
    if not os.path.exists(path):
        sys.exit(f"No extraction file at {path}. Run extract_chapter.py first.")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    chapter_id = data["_meta"]["chapter_id"]
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    alias_index = load_alias_index(conn)

    # name_used_in_text -> resolved character_id
    resolved = {}

    print(f"\n=== Reconciling book {book_order} chapter {chapter_number}: "
          f"{data['_meta']['chapter_title']} ===\n")

    for char in data.get("characters", []):
        name = char.get("name_used_in_text", "").strip()
        if not name:
            continue
        conf = char.get("confidence", "low")
        cid = None

        # 1. exact alias match
        if norm(name) in alias_index:
            cid = alias_index[norm(name)]
            print(f"  [match] '{name}' -> {get_primary_name(conn, cid)}")
            enrich_character(conn, cid, char)

        # 2. the LLM's pointer, verified by name similarity
        elif char.get("likely_matches_existing"):
            ptr = norm(char["likely_matches_existing"])
            if ptr in alias_index:
                cid_candidate = alias_index[ptr]
                # Collect every normalized alias that belongs to the target.
                target_aliases = [
                    a for a, aid in alias_index.items()
                    if aid == cid_candidate
                ]
                name_norm = norm(name)
                best_score = max(
                    (difflib.SequenceMatcher(None, name_norm, a).ratio()
                     for a in target_aliases),
                    default=0.0,
                )
                if best_score >= POINTER_THRESHOLD:
                    cid = cid_candidate
                    print(f"  [llm-match] '{name}' -> "
                          f"{get_primary_name(conn, cid)}")
                    enrich_character(conn, cid, char)
                else:
                    primary = get_primary_name(conn, cid_candidate)
                    note = (
                        f"LLM pointer rejected: incoming name '{name}' was "
                        f"pointed at '{primary}' but best alias similarity "
                        f"is {best_score:.0%} (threshold {POINTER_THRESHOLD:.0%}). "
                        f"Verify manually."
                    )
                    print(f"  [REVIEW] suspicious_llm_match: {note}")
                    queue_review(conn, chapter_id, "suspicious_llm_match",
                                 char, note)
                    continue  # do not fall through to step 3/4

        # 3. new character claimed
        if cid is None and char.get("is_new_character"):
            fid, score = fuzzy_lookup(name, alias_index)
            if fid and score >= FUZZY_THRESHOLD:
                # Looks new to the LLM but is close to an existing name.
                note = (f"LLM says new, but '{name}' is {score:.0%} similar "
                        f"to '{get_primary_name(conn, fid)}'.")
                if auto and conf == "high":
                    cid = create_character(conn, char)
                    print(f"  [new*] '{name}' created (despite {score:.0%} "
                          f"similarity - review later)")
                    queue_review(conn, chapter_id, "possible_duplicate",
                                 char, note)
                else:
                    print(f"  [REVIEW] '{name}' - {note}")
                    queue_review(conn, chapter_id, "possible_duplicate",
                                 char, note)
            else:
                cid = create_character(conn, char)
                print(f"  [new] '{name}' created as a new character")

        # 4. unresolved and not claimed new -> review
        if cid is None:
            fid, score = fuzzy_lookup(name, alias_index)
            suggestion = (f" closest: '{get_primary_name(conn, fid)}' "
                          f"({score:.0%})" if fid else "")
            print(f"  [REVIEW] '{name}' could not be resolved.{suggestion}")
            queue_review(conn, chapter_id, "ambiguous_character", char,
                         f"Unresolved.{suggestion}")
            continue

        resolved[name] = cid
        reconcile_factions(conn, cid, char, chapter_id)

        # refresh index so later characters in the same chapter see new ones
        alias_index = load_alias_index(conn)

    # ----- appearances -----
    app_count = 0
    for app in data.get("appearances", []):
        cid = resolved.get(app.get("character", "").strip())
        if cid is None:
            continue
        alliances = app.get("alliances_shown") or []
        conn.execute(
            """INSERT OR REPLACE INTO appearances
               (character_id, chapter_id, name_used, whereabouts,
                notable_actions, alliances_shown, demeanor)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cid, chapter_id, app.get("character"),
             app.get("whereabouts"), app.get("notable_actions"),
             ", ".join(alliances) if alliances else None,
             app.get("demeanor")),
        )
        app_count += 1
        conn.execute(
            "UPDATE characters SET first_chapter_id = "
            "COALESCE(first_chapter_id, ?) WHERE character_id = ?",
            (chapter_id, cid),
        )

    # ----- relationships -----
    rel_count = 0
    for rel in data.get("relationships", []):
        a = resolved.get(rel.get("character_a", "").strip())
        b = resolved.get(rel.get("character_b", "").strip())
        if a is None or b is None or a == b:
            continue

        lo, hi = sorted((a, b))   # stable ordering for undirected edges
        conn.execute(
            """INSERT OR IGNORE INTO relationships
               (character_a, character_b, relationship_type, directed,
                description, first_chapter_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (lo if not rel.get("directed") else a,
             hi if not rel.get("directed") else b,
             rel.get("relationship_type", "other"),
             1 if rel.get("directed") else 0,
             rel.get("description"), chapter_id),
        )
        rel_count += 1

    conn.execute("UPDATE chapters SET extracted = 1 WHERE chapter_id = ?",
                 (chapter_id,))
    conn.commit()

    pending = conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE resolved = 0"
    ).fetchone()[0]
    print(f"\nCommitted: {app_count} appearances, {rel_count} relationships.")
    print(f"Review queue now holds {pending} unresolved item(s).")
    if pending:
        print("Run  python reconcile.py --review  to see them.")
    conn.close()


def show_review_queue():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT review_id, kind, note, chapter_id FROM review_queue "
        "WHERE resolved = 0 ORDER BY review_id"
    ).fetchall()
    if not rows:
        print("Review queue is empty.")
        return
    print(f"{len(rows)} item(s) awaiting review:\n")
    for rid, kind, note, ch in rows:
        print(f"  #{rid}  [{kind}]  chapter_id={ch}")
        print(f"       {note}\n")
    print("Resolve items by editing the database directly, then set "
          "resolved = 1.")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", type=int, help="series order")
    ap.add_argument("--chapter", type=int, help="chapter number")
    ap.add_argument("--auto", action="store_true",
                    help="auto-commit high-confidence items")
    ap.add_argument("--review", action="store_true",
                    help="list the review queue and exit")
    args = ap.parse_args()

    if args.review:
        show_review_queue()
        return
    if args.book is None or args.chapter is None:
        sys.exit("Provide --book and --chapter, or use --review.")
    reconcile(args.book, args.chapter, args.auto)


if __name__ == "__main__":
    main()
