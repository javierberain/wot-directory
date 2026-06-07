#!/usr/bin/env python3
"""
Unit tests for the alias-canonicalization work in reconcile.py:
  - choose_canonical_name (pure scorer + superset guard + tie/dedup handling)
  - apply_promotion (primary_name promotion, type flips, duplicate guard)
  - add_aliases write-time gate (rank-decorated / descriptor / misnomer drops,
    article-only collapse, protected types).

Runs standalone (`python tests/test_alias_cleanup.py`) or under pytest. Uses an
in-memory DB built from db/schema.sql so the columns match production.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from reconcile import (  # noqa: E402
    choose_canonical_name, apply_promotion, add_aliases, canonical_adopt_text,
)

SCHEMA = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(open(SCHEMA, encoding="utf-8").read())
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _add_char(conn, cid, primary):
    conn.execute("INSERT INTO characters (character_id, primary_name) "
                 "VALUES (?, ?)", (cid, primary))
    conn.execute("INSERT INTO aliases (character_id, alias_text, alias_norm, "
                 "alias_type, is_primary) VALUES (?, ?, ?, 'primary', 1)",
                 (cid, primary, primary.lower().replace("’", "'")))


def _alias(conn, cid, text, atype, is_primary=0):
    conn.execute("INSERT INTO aliases (character_id, alias_text, alias_norm, "
                 "alias_type, is_primary) VALUES (?, ?, ?, ?, ?)",
                 (cid, text, text.lower().replace("’", "'"), atype,
                  is_primary))


def _norms(conn, cid, atype=None):
    if atype:
        return {r[0] for r in conn.execute(
            "SELECT alias_norm FROM aliases WHERE character_id=? "
            "AND alias_type=?", (cid, atype))}
    return {r[0] for r in conn.execute(
        "SELECT alias_norm FROM aliases WHERE character_id=?", (cid,))}


# ── choose_canonical_name ─────────────────────────────────────────────────────

def test_choose_promotes_to_superset_fuller_name():
    assert choose_canonical_name(
        [("Agelmar", "agelmar"), ("Agelmar Jagad", "agelmar jagad")]
    ) == "Agelmar Jagad"


def test_choose_keeps_current_when_not_superset():
    # "Lews Therin Telamon" is fuller but a DIFFERENT name -> keep current.
    cands = [("Rand al'Thor", "rand al'thor"), ("Rand", "rand"),
             ("Lews Therin Telamon", "lews therin telamon")]
    assert choose_canonical_name(cands) == "Rand al'Thor"


def test_choose_dedups_same_name_from_display_and_alias():
    # The fuller name arriving as both given_name and display_name is not a tie.
    cands = [("Egwene", "egwene"), ("Egwene al'Vere", "egwene al'vere"),
             ("Egwene al'Vere", "egwene al'vere")]
    assert choose_canonical_name(cands) == "Egwene al'Vere"


def test_choose_keeps_current_on_genuine_tie():
    # Two DISTINCT supersets at the same score -> ambiguous -> keep current.
    cands = [("Tam", "tam"), ("Tam al'Thor", "tam al'thor"),
             ("Tam al'Vere", "tam al'vere")]
    assert choose_canonical_name(cands) == "Tam"


def test_choose_returns_cleaned_canonical_text():
    # The ADOPTED text is the cleaned form, not the decorated candidate.
    assert choose_canonical_name(
        [("Suroth", "suroth"),
         ("High Lady Suroth Sabelle Meldarath",
          "high lady suroth sabelle meldarath")]
    ) == "Suroth Sabelle Meldarath"
    assert choose_canonical_name(
        [("Elayne", "elayne"),
         ("Elayne of House Trakand", "elayne of house trakand")]
    ) == "Elayne Trakand"


# ── canonical_adopt_text ──────────────────────────────────────────────────────

def test_canonical_adopt_text_strips_rank_and_collapses_house():
    assert canonical_adopt_text("High Lady Suroth Sabelle Meldarath") \
        == "Suroth Sabelle Meldarath"
    assert canonical_adopt_text("the High Lady Alteima") == "Alteima"
    assert canonical_adopt_text("Lord Agelmar") == "Agelmar"
    assert canonical_adopt_text("Lord Captain Commander Pedron Niall") \
        == "Pedron Niall"
    # ' of House ' collapses; original casing/spelling preserved
    assert canonical_adopt_text("Elayne of House Trakand") == "Elayne Trakand"
    assert canonical_adopt_text("Edorion of House Selorna") == "Edorion Selorna"
    # no decoration -> unchanged
    assert canonical_adopt_text("Agelmar Jagad") == "Agelmar Jagad"


def test_canonical_adopt_text_leaves_aiel_and_ogier():
    assert canonical_adopt_text("Sulwin of the Wandering People") \
        == "Sulwin of the Wandering People"
    assert canonical_adopt_text("Loial son of Arent son of Halan") \
        == "Loial son of Arent son of Halan"


# ── apply_promotion ───────────────────────────────────────────────────────────

def test_apply_promotion_flips_primary_and_demotes():
    conn = _db()
    _add_char(conn, 163, "Agelmar")
    _alias(conn, 163, "Agelmar Jagad", "given_name")
    assert apply_promotion(conn, 163) == "Agelmar Jagad"
    assert conn.execute(
        "SELECT primary_name FROM characters WHERE character_id=163"
    ).fetchone()[0] == "Agelmar Jagad"
    prim = conn.execute("SELECT alias_text FROM aliases WHERE character_id=163 "
                        "AND is_primary=1").fetchall()
    assert prim == [("Agelmar Jagad",)]
    types = dict(conn.execute("SELECT alias_text, alias_type FROM aliases "
                              "WHERE character_id=163"))
    assert types["Agelmar"] == "given_name"
    assert types["Agelmar Jagad"] == "primary"


def test_apply_promotion_noop_when_already_fullest():
    conn = _db()
    _add_char(conn, 202, "Verin Mathwin")
    _alias(conn, 202, "Verin", "given_name")
    assert apply_promotion(conn, 202) is None
    assert conn.execute(
        "SELECT primary_name FROM characters WHERE character_id=202"
    ).fetchone()[0] == "Verin Mathwin"


def test_apply_promotion_strips_rank_keeps_decorated_title():
    conn = _db()
    _add_char(conn, 303, "Suroth")
    _alias(conn, 303, "High Lady Suroth Sabelle Meldarath", "given_name")
    assert apply_promotion(conn, 303) == "Suroth Sabelle Meldarath"
    assert conn.execute(
        "SELECT primary_name FROM characters WHERE character_id=303"
    ).fetchone()[0] == "Suroth Sabelle Meldarath"
    types = dict(conn.execute("SELECT alias_text, alias_type FROM aliases "
                              "WHERE character_id=303"))
    assert types["Suroth Sabelle Meldarath"] == "primary"
    assert types["Suroth"] == "given_name"
    # the decorated original survives, re-typed as a title alias
    assert types["High Lady Suroth Sabelle Meldarath"] == "title"
    assert conn.execute("SELECT COUNT(*) FROM aliases WHERE character_id=303 "
                        "AND is_primary=1").fetchone()[0] == 1


def test_apply_promotion_collapses_of_house_keeps_decorated_title():
    conn = _db()
    _add_char(conn, 130, "Elayne")
    _alias(conn, 130, "Elayne of House Trakand", "given_name")
    assert apply_promotion(conn, 130) == "Elayne Trakand"
    types = dict(conn.execute("SELECT alias_text, alias_type FROM aliases "
                              "WHERE character_id=130"))
    assert types["Elayne Trakand"] == "primary"
    assert types["Elayne"] == "given_name"
    assert types["Elayne of House Trakand"] == "title"
    assert conn.execute("SELECT COUNT(*) FROM aliases WHERE character_id=130 "
                        "AND is_primary=1").fetchone()[0] == 1


def test_apply_promotion_blocked_on_duplicate_primary():
    conn = _db()
    _add_char(conn, 1, "Agelmar")
    _alias(conn, 1, "Agelmar Jagad", "given_name")
    _add_char(conn, 2, "Agelmar Jagad")          # the name is already taken
    assert apply_promotion(conn, 1) is None
    assert conn.execute(
        "SELECT primary_name FROM characters WHERE character_id=1"
    ).fetchone()[0] == "Agelmar"
    assert conn.execute(
        "SELECT COUNT(*) FROM review_queue "
        "WHERE kind='promotion_blocked_duplicate'").fetchone()[0] == 1


# ── add_aliases write-time gate ───────────────────────────────────────────────

def test_add_aliases_gate_drops_redundant_keeps_real():
    conn = _db()
    _add_char(conn, 202, "Verin Mathwin")
    _alias(conn, 202, "Verin", "given_name")
    add_aliases(conn, 202, [
        {"alias_text": "Verin Sedai", "alias_type": "title"},
        {"alias_text": "Verin Mathwin Aes Sedai", "alias_type": "title"},
        {"alias_text": "Mistress Mathwin", "alias_type": "title"},
        {"alias_text": "stout little Verin", "alias_type": "epithet"},
        {"alias_text": "Wise One", "alias_type": "epithet",
         "notes": "what Urien mistakenly calls her, though she corrects him"},
        {"alias_text": "Lord of Fal Dara", "alias_type": "title"},   # positional
        {"alias_text": "Eadwina", "alias_type": "disguise"},         # protected
        {"alias_text": "Aes Sedai", "alias_type": "title"},          # generic
    ])
    present = _norms(conn, 202)
    for gone in ["verin sedai", "verin mathwin aes sedai", "mistress mathwin",
                 "stout little verin", "wise one", "aes sedai"]:
        assert gone not in present, gone
    assert "lord of fal dara" in present       # genuine positional title kept
    assert "eadwina" in present                # protected disguise kept


def test_add_aliases_article_collapse_is_order_independent():
    for order in (["the Dark One", "Dark One"], ["Dark One", "the Dark One"]):
        conn = _db()
        _add_char(conn, 2, "Elan Morin Tedronai")
        add_aliases(conn, 2, [{"alias_text": t, "alias_type": "epithet"}
                              for t in order])
        ep = _norms(conn, 2, "epithet")
        assert "the dark one" in ep and "dark one" not in ep, order


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL  {name}: {e}")
    print()
    if failures:
        sys.exit(f"{failures} test(s) failed")
    print("All alias-cleanup tests passed.")
