#!/usr/bin/env python3
"""
Unit tests for reconcile.Roster — the candidate-generation matcher that
replaces the single-difflib-ratio lookup. Uses an in-memory SQLite holding
just the aliases columns Roster reads, so it runs with no API key / dotenv.

Covers the cases that USED to require merge_characters.py / suspicious_llm_match
cleanup afterward:
  - short-vs-full name (Byar -> Jaret Byar, Else -> Else Grinwell)
  - shared-surname ambiguity routed to review instead of a wrong auto-merge
  - stopword-only overlap not treated as a match
  - an LLM pointer trusted only when the names are actually similar
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from reconcile import Roster  # noqa: E402


def make_roster(rows):
    """rows: list of (cid, alias_norm). Returns a Roster over an in-mem DB."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE aliases (character_id INTEGER, alias_norm TEXT)")
    conn.executemany("INSERT INTO aliases VALUES (?, ?)", rows)
    return Roster(conn)


def test_exact_match():
    r = make_roster([(1, "jaret byar"), (1, "byar")])
    assert r.resolve_existing("Byar") == (1, "exact", None)


def test_two_shared_tokens_auto_accept():
    # >=2 shared distinctive tokens is a confident structural match.
    r = make_roster([(50, "lord captain geofram bornhald")])
    cid, method, _ = r.resolve_existing("Geofram Bornhald")
    assert (cid, method) == (50, "token_subset")


def test_single_shared_token_needs_pointer():
    # "Byar" alone shares one token with "Jaret Byar": only a match if the
    # LLM pointer corroborates; otherwise not trusted (would be created new).
    r = make_roster([(10, "jaret byar")])
    assert r.resolve_existing("Byar")[1] == "none"
    cid, method, _ = r.resolve_existing("Byar", pointer="Jaret Byar")
    assert cid == 10 and method == "llm_pointer"


def test_surname_superset_not_over_merged():
    # The bug this fixes: a newcomer with a fuller name must NOT fold into a
    # mononym namesake on a single shared surname token.
    r = make_roster([(480, "lewin")])           # ancestral Aiel "Lewin"
    assert r.resolve_existing("Dannil Lewin")[1] == "none"   # Two Rivers family
    # And the safe direction is equally guarded: "Adan" must not fold into
    # "Governor Adan" on one shared token.
    r2 = make_roster([(60, "governor adan")])
    assert r2.resolve_existing("Adan")[1] == "none"


def test_shared_surname_multiple_is_ambiguous():
    # Several different characters share the one token -> human decides.
    r = make_roster([(40, "tam al'thor"), (41, "rand al'thor")])
    cid, method, cands = r.resolve_existing("al'Thor")
    assert cid is None and method == "ambiguous"
    assert set(cands) == {40, 41}


def test_stopword_only_overlap_is_not_a_match():
    r = make_roster([(50, "lord captain geofram bornhald")])
    cid, method, _ = r.resolve_existing("the Lord")
    assert (cid, method) == (None, "none")


def test_llm_pointer_trusted_only_when_similar():
    r = make_roster([(60, "rand al'thor"), (60, "rand")])
    # Similar spelling, pointer confirms -> accept.
    cid, method, _ = r.resolve_existing("Rand al Thor", pointer="Rand al'Thor")
    assert cid == 60 and method in ("token_subset", "llm_pointer")
    # Unrelated name pointed at Rand -> not trusted, no confident match.
    cid2, method2, _ = r.resolve_existing("Padan Fain", pointer="Rand al'Thor")
    assert cid2 is None and method2 == "none"


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
    print("All reconcile matcher tests passed.")
