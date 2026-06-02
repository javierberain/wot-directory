#!/usr/bin/env python3
"""
Integration tests for reconcile.py's write-time gates and origin handling,
against a fresh schema DB with synthetic extraction JSON. No API/dotenv.

Asserts the headline Phase 2 behaviors:
  - placeholder / collective names are NOT created, but queued with their
    appearance carried in the payload (never silently dropped);
  - generic aliases ("my lord") are skipped while real ones are kept;
  - nationality is normalized on create (Andoran -> Andor);
  - nationality refines coarse->fine across chapters, and a different nation
    is flagged as an origin_conflict without overwriting.
"""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import reconcile  # noqa: E402

SCHEMA = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")


def _setup(tmp):
    db = os.path.join(tmp, "scratch.db")
    conn = sqlite3.connect(db)
    conn.executescript(open(SCHEMA, encoding="utf-8").read())
    conn.execute("INSERT INTO books (series_order, title) VALUES (4, 'TSR')")
    bid = conn.execute("SELECT book_id FROM books").fetchone()[0]
    ch_ids = {}
    for n, title in [(1, "Ch One"), (2, "Ch Two"), (3, "Ch Three")]:
        cur = conn.execute(
            "INSERT INTO chapters (book_id, chapter_number, title, full_text) "
            "VALUES (?, ?, ?, 'x')", (bid, n, title))
        ch_ids[n] = cur.lastrowid
    conn.commit()
    conn.close()
    reconcile.DB_PATH = db
    reconcile.OUT_DIR = tmp
    return db, ch_ids


def _write_extract(tmp, chapter, chapter_id, characters,
                   appearances=None, relationships=None):
    data = {
        "_meta": {"chapter_id": chapter_id, "chapter_title": f"Ch {chapter}"},
        "characters": characters,
        "appearances": appearances or [],
        "relationships": relationships or [],
    }
    with open(os.path.join(tmp, f"b4_c{chapter}.json"), "w",
              encoding="utf-8") as f:
        json.dump(data, f)


def run():
    with tempfile.TemporaryDirectory() as tmp:
        db, ch = _setup(tmp)

        _write_extract(tmp, 1, ch[1], characters=[
            {"name_used_in_text": "Rand al'Thor", "is_new_character": True,
             "confidence": "high", "character_type": "human",
             "stable_traits": {"nationality": "Andoran"},
             "aliases_observed": [
                 {"alias_text": "the Dragon Reborn", "alias_type": "epithet"},
                 {"alias_text": "my lord", "alias_type": "title"}]},
            {"name_used_in_text": "the weaselly man", "is_new_character": True,
             "confidence": "high", "character_type": "human"},
            {"name_used_in_text": "Trollocs", "is_new_character": True,
             "confidence": "high", "character_type": "trolloc"},
        ], appearances=[
            {"character": "Rand al'Thor", "whereabouts": "Two Rivers",
             "notable_actions": "runs", "demeanor": "scared"},
            {"character": "the weaselly man", "whereabouts": "inn",
             "notable_actions": "skulks", "demeanor": "shifty"},
        ])
        reconcile.reconcile(4, 1, auto=True)

        conn = sqlite3.connect(db)

        # Rand created with normalized nationality.
        row = conn.execute("SELECT character_id, nationality FROM characters "
                           "WHERE primary_name = 'Rand al''Thor'").fetchone()
        assert row, "Rand should have been created"
        rand_id, nat = row
        assert nat == "Andor", f"nationality should normalize to Andor, got {nat}"

        # Placeholder + collective NOT created.
        for bad in ("the weaselly man", "Trollocs"):
            assert conn.execute(
                "SELECT 1 FROM characters WHERE primary_name = ?", (bad,)
            ).fetchone() is None, f"{bad} should not be created"

        # Generic alias skipped, real alias kept.
        al = {r[0] for r in conn.execute(
            "SELECT alias_norm FROM aliases WHERE character_id = ?", (rand_id,))}
        assert "the dragon reborn" in al
        assert "my lord" not in al, "generic alias must be skipped"

        # Rand's appearance written; weaselly man's carried in review payload.
        assert conn.execute(
            "SELECT 1 FROM appearances WHERE character_id = ?", (rand_id,)
        ).fetchone() is not None
        kinds = {r[0]: r[1] for r in conn.execute(
            "SELECT kind, payload FROM review_queue")}
        assert "rejected_placeholder" in kinds
        assert "rejected_collective" in kinds
        payload = json.loads(kinds["rejected_placeholder"])
        assert payload["appearance"]["notable_actions"] == "skulks", \
            "placeholder's appearance must be carried, not dropped"

        # Chapter 2: refine Andor -> Emond's Field, Two Rivers, Andor.
        _write_extract(tmp, 2, ch[2], characters=[
            {"name_used_in_text": "Rand al'Thor", "is_new_character": False,
             "confidence": "high", "character_type": "human",
             "stable_traits": {
                 "nationality": "Emond's Field, Two Rivers, Andor"}}])
        reconcile.reconcile(4, 2, auto=True)
        nat2 = conn.execute("SELECT nationality FROM characters WHERE "
                            "character_id = ?", (rand_id,)).fetchone()[0]
        assert nat2 == "Emond's Field, Two Rivers, Andor", \
            f"origin should refine to the city, got {nat2}"

        # Chapter 3: a conflicting nation must NOT overwrite; flags a conflict.
        before = conn.execute("SELECT COUNT(*) FROM review_queue WHERE "
                              "kind = 'origin_conflict'").fetchone()[0]
        _write_extract(tmp, 3, ch[3], characters=[
            {"name_used_in_text": "Rand al'Thor", "is_new_character": False,
             "confidence": "high", "character_type": "human",
             "stable_traits": {"nationality": "Tear"}}])
        reconcile.reconcile(4, 3, auto=True)
        nat3 = conn.execute("SELECT nationality FROM characters WHERE "
                            "character_id = ?", (rand_id,)).fetchone()[0]
        assert nat3 == "Emond's Field, Two Rivers, Andor", \
            "conflicting nation must not overwrite the resolved origin"
        after = conn.execute("SELECT COUNT(*) FROM review_queue WHERE "
                             "kind = 'origin_conflict'").fetchone()[0]
        assert after == before + 1, "origin conflict should be queued"

        conn.close()
    print("All reconcile gate/origin integration tests passed.")


if __name__ == "__main__":
    run()
