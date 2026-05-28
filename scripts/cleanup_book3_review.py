#!/usr/bin/env python3
"""
cleanup_book3_review.py - Resolve the 7 review-queue items from book 3.

Reads the original extraction JSON files for each chapter so we recover
the FULL appearance fields (whereabouts, notable_actions, alliances_shown,
demeanor) and any relationships involving the review-item character.

This mirrors reconcile.py's commit logic for one character at a time,
without re-running the matcher (we already decided each merge/create
manually).

Dry-run by default. Pass --commit to write.

Usage:
    python scripts/cleanup_book3_review.py            # dry-run
    python scripts/cleanup_book3_review.py --commit   # write to db
"""
import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(__file__)
DB_PATH = os.path.join(HERE, "..", "db", "wot.db")
EXTRACT_DIR = os.path.join(HERE, "..", "data", "extractions")


def norm(name):
    return " ".join((name or "").lower().replace("\u2019", "'").split())


# ── Pre-setup: one-off fixes to apply before processing the review queue ─────
# These are not review-queue items; they're DB corrections that make the
# review-queue processing work. Example: adding an alias so a relationship
# endpoint resolves.
PRE_SETUP = [
    {
        "kind": "add_alias",
        "character_id": 77,
        "alias_text": "Geofram Bornhald",
        "alias_type": "given_name",
        "notes": "given name without title prefix; lets prologue "
                 "relationships resolve",
    },
]


# ── Decisions table ──────────────────────────────────────────────────────────
# For each review_id: how to resolve it (merge vs create), and if merge, into
# which existing character_id. Notes attached to created characters here go
# into the description field of the new row.
DECISIONS = [
    {
        "review_id": 63,
        "extract": "b3_c0.json",
        "name_in_text": "Jaret Byar",
        "action": "merge",
        "target_cid": 110,
        "add_alias": ("Jaret Byar", "given_name",
                      "full name introduced in book 3 prologue"),
    },
    {
        "review_id": 64,
        "extract": "b3_c4.json",
        "name_in_text": "the man in dark velvets",
        "action": "create",
        "notes_prefix": "[Dream apparition in Perrin's dream]",
    },
    {
        "review_id": 65,
        "extract": "b3_c4.json",
        "name_in_text": "the beautiful woman",
        "action": "create",
        "notes_prefix": "[Dream apparition in Perrin's dream]",
    },
    {
        "review_id": 66,
        "extract": "b3_c8.json",
        "name_in_text": "Master Harod",
        "action": "merge",
        "target_cid": 328,
        # Already created by the --auto path; review queue says "possible
        # duplicate vs Thom Merrilin", but he's genuinely new. Just need to
        # verify and write the relationships that didn't write the first time.
    },
    {
        "review_id": 67,
        "extract": "b3_c26.json",
        "name_in_text": "Amico",
        "action": "merge",
        "target_cid": 356,
    },
    {
        "review_id": 68,
        "extract": "b3_c31.json",
        "name_in_text": "Mara",
        "action": "dismiss",
        "dismiss_note": "Character within a fable Thom tells; not a real "
                        "person in the WoT world. Not added to directory.",
    },
    {
        "review_id": 69,
        "extract": "b3_c38.json",
        "name_in_text": "Bain",
        "action": "merge",
        "target_cid": 392,
        # Already created by the --auto path. Two Bain<->Chiad relationships
        # already exist; merge will fill in any others that didn't resolve
        # the first time (and idempotently skip the dupes).
    },
]


# ── DB helpers ───────────────────────────────────────────────────────────────

def load_alias_index(conn):
    """{normalized_alias: character_id} for all known aliases."""
    return {a: cid for cid, a in conn.execute(
        "SELECT character_id, alias_norm FROM aliases"
    )}


def resolve_name_to_cid(conn, alias_index, name, local_resolutions):
    """Resolve an extracted name to a character_id.

    Lookup order:
      1. local_resolutions  (characters we just created/merged in this run)
      2. alias_index        (everything already committed in the DB)

    Returns (cid, source) where source is 'local', 'db', or None.
    """
    if not name:
        return None, None
    nnorm = norm(name)
    if nnorm in local_resolutions:
        return local_resolutions[nnorm], "local"
    if nnorm in alias_index:
        return alias_index[nnorm], "db"
    return None, None


# ── Writers (each respects dry-run) ──────────────────────────────────────────

def add_alias(conn, cid, text, atype, notes, dry):
    nnorm = norm(text)
    existing = conn.execute(
        "SELECT alias_id FROM aliases WHERE character_id = ? AND alias_norm = ?",
        (cid, nnorm),
    ).fetchone()
    if existing:
        print(f"    ALIAS exists: '{text}' on char {cid}")
        return
    print(f"    ALIAS add: '{text}' [{atype}] -> char {cid}")
    if not dry:
        conn.execute(
            "INSERT INTO aliases (character_id, alias_text, alias_norm, "
            "alias_type, notes) VALUES (?, ?, ?, ?, ?)",
            (cid, text, nnorm, atype, notes),
        )


def enrich_character(conn, cid, char_payload, dry):
    """Fill empty stable-trait columns from the character payload."""
    st = char_payload.get("stable_traits") or {}
    for col in ("nationality", "physical_traits", "age",
                "filiations", "personality"):
        val = st.get(col)
        if not val:
            continue
        current = conn.execute(
            f"SELECT {col} FROM characters WHERE character_id = ?", (cid,)
        ).fetchone()[0]
        if current:
            continue
        snippet = val[:60] + ("..." if len(val) > 60 else "")
        print(f"    ENRICH char {cid}: set {col} = '{snippet}'")
        if not dry:
            conn.execute(
                f"UPDATE characters SET {col} = ?, "
                f"updated_at = datetime('now') WHERE character_id = ?",
                (val, cid),
            )


def create_character(conn, char_payload, dry, notes_prefix=None):
    primary = char_payload["name_used_in_text"]
    st = char_payload.get("stable_traits") or {}
    ctype = char_payload.get("character_type", "human") or "human"
    description = char_payload.get("notes")
    if notes_prefix:
        description = f"{notes_prefix} {description or ''}".strip()

    print(f"    CREATE char: primary_name='{primary}' type={ctype}")
    if dry:
        return -1

    cur = conn.execute(
        """INSERT INTO characters
           (primary_name, character_type, nationality, physical_traits,
            age, filiations, personality, description)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (primary, ctype,
         st.get("nationality"), st.get("physical_traits"),
         st.get("age"), st.get("filiations"),
         st.get("personality"), description),
    )
    cid = cur.lastrowid
    conn.execute(
        """INSERT OR IGNORE INTO aliases
           (character_id, alias_text, alias_norm, alias_type, is_primary)
           VALUES (?, ?, ?, 'primary', 1)""",
        (cid, primary, norm(primary)),
    )
    for a in char_payload.get("aliases_observed", []) or []:
        text = (a.get("alias_text") or "").strip()
        if not text:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO aliases
               (character_id, alias_text, alias_norm, alias_type, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (cid, text, norm(text),
             a.get("alias_type", "nickname"), a.get("notes")),
        )
    return cid


def commit_factions(conn, cid, char_payload, chapter_id, dry):
    for fac in char_payload.get("factions", []) or []:
        name = (fac.get("name") or "").strip()
        if not name:
            continue
        nnorm = norm(name)
        ftype = fac.get("faction_type", "other") or "other"
        existing = conn.execute(
            "SELECT faction_id FROM factions WHERE name_norm = ?", (nnorm,)
        ).fetchone()
        if existing:
            fid = existing[0]
            print(f"    FACTION exists: '{name}' (faction_id={fid})")
        else:
            print(f"    FACTION create: '{name}' [{ftype}]")
            if not dry:
                cur = conn.execute(
                    "INSERT INTO factions (name, name_norm, faction_type) "
                    "VALUES (?, ?, ?)", (name, nnorm, ftype),
                )
                fid = cur.lastrowid
            else:
                fid = -1
        role = (fac.get("role") or "member").strip().lower() or "member"
        if cid > 0 and fid > 0:
            print(f"    FACTION join: char {cid} -> faction {fid} role={role}")
            if not dry:
                conn.execute(
                    """INSERT OR IGNORE INTO character_factions
                       (character_id, faction_id, role, first_chapter_id, notes)
                       VALUES (?, ?, ?, ?, ?)""",
                    (cid, fid, role, chapter_id, fac.get("notes")),
                )
                if role == "leader":
                    conn.execute(
                        "UPDATE character_factions SET role = 'leader' "
                        "WHERE character_id = ? AND faction_id = ?",
                        (cid, fid),
                    )
        else:
            print(f"    FACTION join: char {cid} -> faction {fid} "
                  f"role={role}  (dry-run, placeholders)")


def commit_appearance(conn, cid, chapter_id, app_dict, char_payload, dry):
    """Write a full appearance row using the matching appearance dict from
    the extraction JSON. Falls back to a thin row if not found."""
    if app_dict is None:
        print(f"    APPEARANCE: char={cid if cid > 0 else '<new>'} "
              f"ch={chapter_id}  (NO matching appearance dict in JSON, "
              f"writing thin row)")
        name_used = char_payload.get("name_used_in_text")
        notable = char_payload.get("notes")
        if not dry and cid > 0:
            conn.execute(
                """INSERT OR REPLACE INTO appearances
                   (character_id, chapter_id, name_used, notable_actions)
                   VALUES (?, ?, ?, ?)""",
                (cid, chapter_id, name_used, notable),
            )
        return

    alliances = app_dict.get("alliances_shown") or []
    print(f"    APPEARANCE: char={cid if cid > 0 else '<new>'} "
          f"ch={chapter_id} name_used='{app_dict.get('character')}'")
    print(f"      whereabouts: "
          f"{(app_dict.get('whereabouts') or '')[:80]}")
    print(f"      actions: "
          f"{(app_dict.get('notable_actions') or '')[:80]}")
    print(f"      demeanor: "
          f"{(app_dict.get('demeanor') or '')[:80]}")
    if alliances:
        print(f"      alliances: {', '.join(alliances)[:80]}")
    if not dry and cid > 0:
        conn.execute(
            """INSERT OR REPLACE INTO appearances
               (character_id, chapter_id, name_used, whereabouts,
                notable_actions, alliances_shown, demeanor)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cid, chapter_id, app_dict.get("character"),
             app_dict.get("whereabouts"), app_dict.get("notable_actions"),
             ", ".join(alliances) if alliances else None,
             app_dict.get("demeanor")),
        )


def commit_relationships(conn, target_cid, target_name, extract_data,
                          chapter_id, alias_index, local_resolutions, dry):
    """Write every relationship in this chapter that involves target_name.

    The other endpoint is resolved via local_resolutions (just created/merged)
    or alias_index (already in DB). If unresolvable, the relationship is
    skipped and reported.
    """
    rels = extract_data.get("relationships", [])
    target_nnorm = norm(target_name)
    matched = [r for r in rels
               if norm(r.get("character_a")) == target_nnorm
               or norm(r.get("character_b")) == target_nnorm]
    if not matched:
        return

    print(f"    relationships involving '{target_name}': {len(matched)}")
    for rel in matched:
        a_name = rel.get("character_a", "").strip()
        b_name = rel.get("character_b", "").strip()
        rtype = rel.get("relationship_type", "other")
        directed = bool(rel.get("directed"))
        desc = rel.get("description")

        a_cid, a_src = resolve_name_to_cid(
            conn, alias_index, a_name, local_resolutions)
        b_cid, b_src = resolve_name_to_cid(
            conn, alias_index, b_name, local_resolutions)

        if a_cid is None or b_cid is None or a_cid == b_cid:
            missing = []
            if a_cid is None: missing.append(f"'{a_name}'")
            if b_cid is None: missing.append(f"'{b_name}'")
            if missing:
                print(f"      SKIP rel [{rtype}]: unresolved "
                      f"endpoint(s): {', '.join(missing)}")
            else:
                print(f"      SKIP rel [{rtype}]: self-loop "
                      f"('{a_name}' == '{b_name}')")
            continue

        # Stable ordering for undirected, original ordering for directed.
        if directed:
            ca, cb = a_cid, b_cid
        else:
            ca, cb = sorted((a_cid, b_cid))

        existing = conn.execute(
            "SELECT relationship_id FROM relationships "
            "WHERE character_a = ? AND character_b = ? "
            "AND relationship_type = ?",
            (ca, cb, rtype),
        ).fetchone()
        if existing:
            print(f"      REL exists: [{rtype}] '{a_name}' ({a_cid}, "
                  f"{a_src}) <-> '{b_name}' ({b_cid}, {b_src})")
            continue
        print(f"      REL add: [{rtype} directed={directed}] "
              f"'{a_name}' ({a_cid}, {a_src}) -> '{b_name}' "
              f"({b_cid}, {b_src})")
        print(f"        desc: {(desc or '')[:100]}")
        if not dry:
            conn.execute(
                """INSERT OR IGNORE INTO relationships
                   (character_a, character_b, relationship_type, directed,
                    description, first_chapter_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ca, cb, rtype, 1 if directed else 0, desc, chapter_id),
            )


def mark_resolved(conn, review_id, dry):
    print(f"    RESOLVE: review_id={review_id}")
    if not dry:
        conn.execute(
            "UPDATE review_queue SET resolved = 1 WHERE review_id = ?",
            (review_id,),
        )


# ── Per-item processing ──────────────────────────────────────────────────────

def process(conn, decision, dry, local_resolutions):
    review_id = decision["review_id"]
    extract_path = os.path.join(EXTRACT_DIR, decision["extract"])

    if not os.path.exists(extract_path):
        print(f"\n#{review_id}: SKIP - extraction file not found: "
              f"{extract_path}")
        return

    with open(extract_path, encoding="utf-8") as f:
        extract = json.load(f)

    chapter_id = extract["_meta"]["chapter_id"]
    name = decision["name_in_text"]

    # Find this character's character dict + appearance dict in the JSON.
    char_payload = None
    for c in extract.get("characters", []):
        if norm(c.get("name_used_in_text")) == norm(name):
            char_payload = c
            break
    if char_payload is None:
        # Fall back to the queue payload.
        row = conn.execute(
            "SELECT payload FROM review_queue WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        if row:
            char_payload = json.loads(row[0])
            print(f"\n#{review_id}: char dict not in JSON, using queue "
                  f"payload as fallback")

    app_dict = None
    for a in extract.get("appearances", []):
        if norm(a.get("character")) == norm(name):
            app_dict = a
            break

    action = decision["action"]
    print(f"\n#{review_id} '{name}' -> {action} "
          f"(extraction: {decision['extract']}, chapter_id={chapter_id})")

    if action == "dismiss":
        note = decision.get("dismiss_note", "Dismissed.")
        print(f"    DISMISS: {note}")
        mark_resolved(conn, review_id, dry)
        return

    alias_index = load_alias_index(conn)

    if action == "merge":
        cid = decision["target_cid"]
        if "add_alias" in decision:
            atext, atype, anotes = decision["add_alias"]
            add_alias(conn, cid, atext, atype, anotes, dry)
        enrich_character(conn, cid, char_payload, dry)
        commit_factions(conn, cid, char_payload, chapter_id, dry)
        commit_appearance(conn, cid, chapter_id, app_dict, char_payload, dry)
        # Update local index so other items in this run can resolve to this cid.
        local_resolutions[norm(name)] = cid
        commit_relationships(conn, cid, name, extract, chapter_id,
                             alias_index, local_resolutions, dry)
    elif action == "create":
        cid = create_character(
            conn, char_payload, dry,
            notes_prefix=decision.get("notes_prefix"),
        )
        if cid > 0 or dry:
            commit_factions(conn, cid, char_payload, chapter_id, dry)
            commit_appearance(conn, cid, chapter_id, app_dict,
                              char_payload, dry)
            if cid > 0:
                local_resolutions[norm(name)] = cid
                # Also register every alias_observed entry under this cid,
                # so relationships referring to this character by alternate
                # name still resolve.
                for a in char_payload.get("aliases_observed", []) or []:
                    text = (a.get("alias_text") or "").strip()
                    if text:
                        local_resolutions[norm(text)] = cid
                commit_relationships(conn, cid, name, extract, chapter_id,
                                     alias_index, local_resolutions, dry)
            else:
                # Dry-run: relationships need a cid to print sensibly.
                print(f"    (dry-run: relationships skipped because "
                      f"new character has no real id yet; commit run "
                      f"will write them)")
    mark_resolved(conn, review_id, dry)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="actually write (default: dry-run)")
    args = ap.parse_args()
    dry = not args.commit

    banner = "DRY RUN - no changes will be made. Pass --commit to write." \
        if dry else "COMMIT MODE - changes will be written to the database."
    print("=" * 60)
    print(banner)
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    local_resolutions = {}

    try:
        if PRE_SETUP:
            print("\n--- pre-setup ---")
            for item in PRE_SETUP:
                if item["kind"] == "add_alias":
                    add_alias(
                        conn,
                        item["character_id"],
                        item["alias_text"],
                        item["alias_type"],
                        item.get("notes"),
                        dry,
                    )
                else:
                    print(f"    WARNING: unknown pre-setup kind: "
                          f"{item['kind']}")

        for decision in DECISIONS:
            process(conn, decision, dry, local_resolutions)
        if not dry:
            conn.commit()
            print("\nCOMMITTED.")
        else:
            print("\nDry-run complete. Re-run with --commit to apply.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
