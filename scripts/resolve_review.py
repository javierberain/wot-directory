#!/usr/bin/env python3
"""
resolve_review.py - Apply human decisions to review_queue items, recovering the
FULL appearance + relationships from each item's stored payload.

This is the generic replacement for the per-book cleanup_bookN_review.py
scripts. Because reconcile.py now stores a self-recovering payload
({character, appearance, relationships}) on every queued item, a single
data-driven tool can resolve any book's queue: it never has to re-read the
extraction JSON by hand, and the dropped appearance is written back in.

Workflow:
    # 1. See what's open and copy the ids into a decisions file.
    python scripts/resolve_review.py --list

    # 2. Write a decisions JSON (see format below), then dry-run it.
    python scripts/resolve_review.py --decisions data/review_decisions.json

    # 3. Commit.
    python scripts/resolve_review.py --decisions data/review_decisions.json --commit

Decisions file: a JSON list, one object per review_id:
    [
      {"review_id": 63, "action": "merge", "target": "Byar",
       "add_alias": ["Jaret Byar", "given_name", "full name from prologue"]},
      {"review_id": 64, "action": "create", "rename": "the man in dark velvets",
       "notes_prefix": "[Dream apparition in Perrin's dream]"},
      {"review_id": 68, "action": "dismiss", "note": "fable character, not real"}
    ]

  action "merge"   : fold the item onto an existing row. `target` is a primary
                     name or `target_cid` is the id. Optional `add_alias`
                     [text, type, notes] is added first so endpoints resolve.
  action "create"  : create a new row from the payload. Optional `rename`
                     overrides the primary name (e.g. to give an unnamed
                     walk-on a real name); optional `notes_prefix` is prepended
                     to the description.
  action "dismiss" : mark resolved with no DB change (not a real character).

Dry-run by default; --commit writes. A backup is taken before the first write.
"""
import argparse
import json
import os
import pathlib
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
from reconcile import (  # noqa: E402
    Roster, norm, create_character, add_aliases, enrich_character,
    reconcile_factions,
)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "wot.db")


def _payload_parts(payload):
    """Return (char, appearance, relationships) from a review payload,
    tolerating both the new enriched shape and a legacy bare-character dict."""
    if isinstance(payload, dict) and "character" in payload:
        return (payload.get("character") or {}, payload.get("appearance"),
                payload.get("relationships") or [])
    return payload or {}, None, []   # legacy: payload was the char dict itself


def take_backup(db_path):
    p = pathlib.Path(db_path).resolve()
    bak = pathlib.Path(str(p) + ".pre-resolve-review.bak")
    shutil.copy2(p, bak)
    for ext in ("-wal", "-shm"):
        s = pathlib.Path(str(p) + ext)
        if s.exists():
            shutil.copy2(s, pathlib.Path(str(bak) + ext))
    print(f"Backup written: {bak}")


def list_queue(conn):
    rows = conn.execute(
        "SELECT review_id, kind, note, chapter_id, payload FROM review_queue "
        "WHERE resolved = 0 ORDER BY review_id"
    ).fetchall()
    if not rows:
        print("Review queue is empty.")
        return
    print(f"{len(rows)} open item(s):\n")
    for rid, kind, note, ch, payload in rows:
        char, app, rels = _payload_parts(json.loads(payload))
        name = char.get("name_used_in_text", "?")
        print(f"  #{rid}  [{kind}]  '{name}'  chapter_id={ch}")
        print(f"       {note}")
        print(f"       payload: appearance={'yes' if app else 'no'}, "
              f"relationships={len(rels)}\n")


def commit_appearance(conn, cid, chapter_id, app, char, dry):
    """Write the full appearance row from the payload (thin fallback if absent)."""
    if app is None:
        print(f"      appearance: (none in payload; writing thin row)")
        if not dry:
            conn.execute(
                "INSERT OR REPLACE INTO appearances "
                "(character_id, chapter_id, name_used, notable_actions) "
                "VALUES (?, ?, ?, ?)",
                (cid, chapter_id, char.get("name_used_in_text"),
                 char.get("notes")))
        return
    alliances = app.get("alliances_shown") or []
    print(f"      appearance: name_used='{app.get('character')}' "
          f"demeanor='{(app.get('demeanor') or '')[:40]}'")
    if not dry:
        conn.execute(
            "INSERT OR REPLACE INTO appearances "
            "(character_id, chapter_id, name_used, whereabouts, "
            "notable_actions, alliances_shown, demeanor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, chapter_id, app.get("character"), app.get("whereabouts"),
             app.get("notable_actions"),
             ", ".join(alliances) if alliances else None, app.get("demeanor")))


def commit_relationships(conn, roster, target_name, rels, chapter_id,
                         local, dry):
    """Write relationships whose BOTH endpoints resolve confidently (existing
    roster or a row created/merged in this run). Unresolvable edges are skipped
    and reported."""
    for rel in rels:
        a, b = (rel.get("character_a") or "").strip(), \
               (rel.get("character_b") or "").strip()
        ca = local.get(norm(a)) or roster.resolve_existing(a)[0]
        cb = local.get(norm(b)) or roster.resolve_existing(b)[0]
        rtype = rel.get("relationship_type", "other")
        if ca is None or cb is None or ca == cb:
            print(f"      SKIP rel [{rtype}] '{a}'<->'{b}': "
                  f"unresolved/self-loop")
            continue
        directed = bool(rel.get("directed"))
        x, y = (ca, cb) if directed else tuple(sorted((ca, cb)))
        print(f"      rel [{rtype}] {a}({ca}) -> {b}({cb})")
        if not dry:
            conn.execute(
                "INSERT OR IGNORE INTO relationships "
                "(character_a, character_b, relationship_type, directed, "
                "description, first_chapter_id) VALUES (?, ?, ?, ?, ?, ?)",
                (x, y, rtype, 1 if directed else 0,
                 rel.get("description"), chapter_id))


def resolve_target(conn, dec):
    if dec.get("target_cid"):
        return dec["target_cid"]
    name = dec.get("target")
    if not name:
        sys.exit(f"#{dec['review_id']}: merge needs 'target' or 'target_cid'.")
    row = conn.execute(
        "SELECT character_id FROM characters WHERE primary_name = ?", (name,)
    ).fetchone()
    if not row:
        sys.exit(f"#{dec['review_id']}: no character with primary_name "
                 f"'{name}'. Use target_cid or fix the name.")
    return row[0]


def process(conn, dec, dry):
    rid = dec["review_id"]
    row = conn.execute(
        "SELECT kind, note, chapter_id, payload, resolved FROM review_queue "
        "WHERE review_id = ?", (rid,)).fetchone()
    if not row:
        print(f"\n#{rid}: not found, skipping.")
        return
    kind, note, chapter_id, payload, resolved = row
    if resolved:
        print(f"\n#{rid}: already resolved, skipping.")
        return
    char, app, rels = _payload_parts(json.loads(payload))
    action = dec["action"]
    print(f"\n#{rid} [{kind}] '{char.get('name_used_in_text')}' -> {action}")

    if action == "dismiss":
        print(f"      dismiss: {dec.get('note', note)}")
        if not dry:
            conn.execute("UPDATE review_queue SET resolved = 1 "
                         "WHERE review_id = ?", (rid,))
        return

    roster = Roster(conn)
    local = {}

    if action == "merge":
        cid = resolve_target(conn, dec)
        print(f"      merge into character_id={cid}")
        if dec.get("add_alias"):
            text, atype, anotes = (dec["add_alias"] + [None, None])[:3]
            add_aliases(conn, cid, [{"alias_text": text, "alias_type": atype,
                                     "notes": anotes}])
        if not dry:
            for k, n in enrich_character(conn, cid, char, chapter_id):
                print(f"      note: {k}: {n}")
            reconcile_factions(conn, cid, char, chapter_id)
    elif action == "create":
        if dec.get("rename"):
            char["name_used_in_text"] = dec["rename"]
        if dec.get("notes_prefix"):
            char["notes"] = f"{dec['notes_prefix']} {char.get('notes') or ''}"\
                .strip()
        print(f"      create primary_name='{char['name_used_in_text']}'")
        cid = -1
        if not dry:
            cid = create_character(conn, char)
            reconcile_factions(conn, cid, char, chapter_id)
    else:
        sys.exit(f"#{rid}: unknown action '{action}'.")

    # Register every name this character is known by so relationship endpoints
    # referring to it resolve even before the roster is rebuilt.
    if cid and cid > 0:
        local[norm(char.get("name_used_in_text", ""))] = cid
        for a in char.get("aliases_observed", []) or []:
            t = (a.get("alias_text") or "").strip()
            if t:
                local[norm(t)] = cid
        roster = Roster(conn)

    name = char.get("name_used_in_text", "")
    commit_appearance(conn, cid if cid else -1, chapter_id, app, char, dry)
    commit_relationships(conn, roster, name, rels, chapter_id, local, dry)
    if not dry:
        conn.execute("UPDATE review_queue SET resolved = 1 "
                     "WHERE review_id = ?", (rid,))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--list", action="store_true",
                    help="list open review items and exit")
    ap.add_argument("--decisions", help="path to a decisions JSON file")
    ap.add_argument("--commit", action="store_true",
                    help="write changes (default: dry-run)")
    args = ap.parse_args()
    db_path = args.db

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    if args.list or not args.decisions:
        list_queue(conn)
        conn.close()
        return

    decisions = json.load(open(args.decisions, encoding="utf-8"))
    dry = not args.commit
    print("=" * 60)
    print("DRY RUN - no changes (pass --commit to write)." if dry
          else "COMMIT MODE - writing changes.")
    print("=" * 60)
    if not dry:
        take_backup(db_path)
    try:
        for dec in decisions:
            process(conn, dec, dry)
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
