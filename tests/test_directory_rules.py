#!/usr/bin/env python3
"""
Unit tests for scripts/directory_rules.py — the shared validation/normalization
rules. Runs standalone (`python tests/test_directory_rules.py`) or under pytest.

Covers the two nationality bugs the refactor exists to fix:
  - the "unknown" trap (placeholder strings must never count as filled), and
  - coarse values blocking later refinement (Andor -> Emond's Field, ...).
Plus the alias/placeholder gates and the schema-CHECK sync guard.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import directory_rules as dr  # noqa: E402

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")


# ── Alias quality ─────────────────────────────────────────────────────────────

def test_generic_alias_gate():
    for good in ["Rand al'Thor", "Verin Sedai", "Else", "the Dragon Reborn"]:
        assert not dr.is_generic_alias(good), good
    for bad in ["Aes Sedai", "the girl", "girl", "my lord", "What", "sister",
                "the man", "mistress"]:
        assert dr.is_generic_alias(bad), bad


def test_suspect_alias_is_advisory_only():
    # "What" is hard-caught; "Else"/"True" collide with real names and must NOT
    # be auto-rejected — only surfaced as suspect for human review.
    assert dr.looks_like_suspect_alias("what")
    assert not dr.is_generic_alias("Else")
    assert not dr.looks_like_suspect_alias("Else")


# ── Titles / ranks / descriptors (alias canonicalization) ─────────────────────

def test_strip_titles():
    assert dr.strip_titles("lord of fal dara") == "of fal dara"
    assert dr.strip_titles("verin mathwin aes sedai") == "verin mathwin"
    assert dr.strip_titles("mistress mathwin") == "mathwin"
    assert dr.strip_titles("the high lady alteima") == "alteima"
    assert dr.strip_titles("agelmar dai shan") == "agelmar"
    assert dr.strip_titles("verin sedai") == "verin"
    # no decoration -> unchanged
    assert dr.strip_titles("agelmar jagad") == "agelmar jagad"


def test_match_key():
    assert dr.match_key("Verin Sedai") == "verin"
    assert dr.match_key("Lord Agelmar") == "agelmar"
    assert dr.match_key("the High Lady Alteima") == "alteima"
    assert dr.match_key("Agelmar Jagad") == "agelmar jagad"


def test_is_rank_decorated_redundant():
    nts = {"verin", "mathwin"}
    for bad in ["verin sedai", "verin aes sedai", "verin mathwin aes sedai",
                "mistress mathwin"]:
        assert dr.is_rank_decorated_redundant(bad, nts), bad
    assert dr.is_rank_decorated_redundant("lord agelmar", {"agelmar", "jagad"})
    assert dr.is_rank_decorated_redundant("agelmar dai shan", {"agelmar", "jagad"})
    assert dr.is_rank_decorated_redundant("high lady alteima", {"alteima"})
    # SPARED: positional/singular titles whose residue isn't a name subset
    assert not dr.is_rank_decorated_redundant("lord of fal dara",
                                              {"agelmar", "jagad"})
    assert not dr.is_rank_decorated_redundant("the amyrlin seat",
                                              {"siuan", "sanche"})
    # residue empty (pure rank word) is not "redundant" here
    assert not dr.is_rank_decorated_redundant("mother", {"siuan", "sanche"})


def test_is_descriptor_epithet():
    assert dr.is_descriptor_epithet("stout little verin", {"verin", "mathwin"})
    assert dr.is_descriptor_epithet("the stout verin", {"verin"})
    # SPARED: no name token (a real unique epithet)
    assert not dr.is_descriptor_epithet("the dragon reborn", {"rand", "al'thor"})
    # SPARED: name but no descriptor adjective
    assert not dr.is_descriptor_epithet("lord agelmar", {"agelmar", "jagad"})
    # SPARED: descriptor + extra non-name/non-descriptor word
    assert not dr.is_descriptor_epithet("stout verin of cairhien",
                                        {"verin", "mathwin"})


def test_is_corrected_misnomer():
    for note in ["mistakenly called the Amyrlin", "wrongly named",
                 "incorrectly identified", "called in error",
                 "not actually her name", "the text corrects this",
                 "confused with her sister"]:
        assert dr.is_corrected_misnomer(note), note
    for note in [None, "", "a title she holds", "her given name"]:
        assert not dr.is_corrected_misnomer(note), note


# ── Placeholder / collective primary names ────────────────────────────────────

def test_placeholder_names():
    for ph in ["the weaselly man", "a serving woman", "the woman with the dagger",
               "the innkeeper", "the gleeman"]:
        assert dr.is_placeholder_name(ph), ph
    for ok in ["Rand al'Thor", "the Dark One", "Narg"]:
        assert not dr.is_placeholder_name(ok), ok


def test_collective_names():
    assert dr.is_collective_name("Trollocs", "trolloc")
    assert dr.is_collective_name("the Myrddraal", "myrddraal")
    assert not dr.is_collective_name("Narg", "trolloc")     # named individual
    assert not dr.is_collective_name("Bela", "horse")


def test_rejection_reason():
    assert dr.rejection_reason("the weaselly man", "human") == "placeholder"
    assert dr.rejection_reason("Trollocs", "trolloc") == "collective"
    assert dr.rejection_reason("the Amyrlin Seat", "human") == "title"
    assert dr.rejection_reason("Rand al'Thor", "human") is None


# ── Nationality: the "unknown" trap ───────────────────────────────────────────

def test_unresolved_origin():
    for placeholder in [None, "", "  ", "unknown", "Unknown",
                        "unknown (not Two Rivers)", "unclear", "n/a"]:
        assert dr.is_unresolved_origin(placeholder), repr(placeholder)
    for real in ["Andor", "Two Rivers, Andor", "Aiel"]:
        assert not dr.is_unresolved_origin(real), real


def test_normalize_strips_placeholders_and_demonyms():
    assert dr.normalize_nationality("unknown") is None
    assert dr.normalize_nationality("") is None
    assert dr.normalize_nationality("Andor (presumably)") == "Andor"
    assert dr.normalize_nationality("Andoran") == "Andor"
    assert dr.normalize_nationality("Cairhienin") == "Cairhien"
    assert dr.normalize_nationality("Tairen") == "Tear"
    assert dr.normalize_nationality("Aiel") == "Aiel"
    # compound demonym normalisation per component
    assert dr.normalize_nationality("Emond's Field, Two Rivers, Andoran") \
        == "Emond's Field, Two Rivers, Andor"


# ── Origin: region -> nation completion + taxonomy ────────────────────────────

def test_region_completes_to_nation():
    assert dr.normalize_nationality("Two Rivers") == "Two Rivers, Andor"
    assert dr.normalize_nationality("Emond's Field") \
        == "Emond's Field, Two Rivers, Andor"
    assert dr.normalize_nationality("Maule") == "Maule, Tear"
    # already-complete values are unchanged (idempotent)
    assert dr.normalize_nationality("Two Rivers, Andor") == "Two Rivers, Andor"
    assert dr.normalize_nationality("Emond's Field, Two Rivers, Andor") \
        == "Emond's Field, Two Rivers, Andor"


def test_classify_origin():
    assert dr.classify_origin("Narg", "trolloc") == "Shadow"
    assert dr.classify_origin("a Myrddraal", "myrddraal") == "Shadow"
    assert dr.classify_origin("the Dark One", "other") == "Time"
    assert dr.classify_origin("Machin Shin", "other") == "Time"
    assert dr.classify_origin("Rand al'Thor", "human") is None


# ── Nationality: coarse -> fine refinement ────────────────────────────────────

def test_refine_fills_unresolved_including_legacy_unknown():
    assert dr.refine_nationality(None, "Andor") == ("Andor", False)
    assert dr.refine_nationality("unknown", "Andoran") == ("Andor", False)


def test_refine_upgrades_to_more_specific():
    val, conflict = dr.refine_nationality(
        "Andor", "Emond's Field, Two Rivers, Andor")
    assert val == "Emond's Field, Two Rivers, Andor"
    assert conflict is False


def test_refine_keeps_more_specific_when_coarser_arrives():
    val, conflict = dr.refine_nationality(
        "Emond's Field, Two Rivers, Andor", "Andor")
    assert val == "Emond's Field, Two Rivers, Andor"
    assert conflict is False


def test_refine_region_only_is_not_a_conflict():
    # The bug behind 189 false conflicts: "Two Rivers" (region, no nation) is a
    # coarser reference to "Two Rivers, Andor", NOT a different place.
    val, conflict = dr.refine_nationality("Two Rivers, Andor", "Two Rivers")
    assert val == "Two Rivers, Andor"
    assert conflict is False
    # and the region-first refinement still upgrades
    val2, c2 = dr.refine_nationality(
        "Two Rivers", "Emond's Field, Two Rivers, Andor")
    assert val2 == "Emond's Field, Two Rivers, Andor" and c2 is False


def test_refine_flags_conflict_on_different_nation():
    val, conflict = dr.refine_nationality("Andor", "Tear")
    assert val == "Andor"
    assert conflict is True


def test_refine_noop_when_incoming_unresolved():
    assert dr.refine_nationality("Andor", "unknown") == ("Andor", False)


# ── Schema sync guard ─────────────────────────────────────────────────────────

def test_char_types_match_schema_check():
    """VALID_CHAR_TYPES must equal the character_type CHECK set in schema.sql,
    so the validator can never silently disagree with the DB constraint."""
    sql = open(SCHEMA_PATH, encoding="utf-8").read()
    m = re.search(r"character_type[^(]*IN\s*\(([^)]*)\)", sql, re.IGNORECASE | re.DOTALL)
    assert m, "could not find character_type CHECK in schema.sql"
    schema_types = set(re.findall(r"'([^']+)'", m.group(1)))
    assert schema_types == dr.VALID_CHAR_TYPES, (schema_types, dr.VALID_CHAR_TYPES)


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
    print("All directory_rules tests passed.")
