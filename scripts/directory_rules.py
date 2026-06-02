#!/usr/bin/env python3
"""
directory_rules.py - The single source of truth for the WoT directory's
validation and normalization rules.

For books 1-3 the same rules were duplicated across files: `norm()` was
copy-pasted into reconcile.py / hygiene_audit.py / cleanup_book3_review.py,
the valid character-type set lived in reconcile.py, the placeholder/collective/
generic-alias wordlists lived only in hygiene_audit.py (so they ran AFTER bad
rows were already written), and the nationality conventions lived only in the
README. This module pulls all of it into one place so the extractor prompt,
the reconciler's write-time gates, the auditor's reports, and the retrofit
tools all apply IDENTICAL logic.

Nothing here touches a database or calls an API. Pure functions + constants.
"""

import re

# ── Normalisation ─────────────────────────────────────────────────────────────


def norm(name):
    """Lowercase, straight-apostrophe, whitespace-collapsed key for matching.

    Identical to the norm() that was copy-pasted across reconcile.py and
    hygiene_audit.py — now defined once.
    """
    return " ".join((name or "").lower().replace("’", "'").split())


# ── Valid enum values (kept in sync with db/schema.sql CHECK constraints) ─────
# A mismatch between these and the schema CHECK aborts a reconcile commit; a
# mismatch between these and the extractor prompt silently coerces data to
# 'other'. test_directory_rules.py asserts VALID_CHAR_TYPES equals the schema
# CHECK set so drift is caught automatically rather than by README discipline.

VALID_CHAR_TYPES = {
    "human", "ogier", "trolloc", "myrddraal", "horse", "wolf", "other",
}
VALID_FACTION_TYPES = {
    "ajah", "order", "house", "clan", "society", "other",
}


def coerce_char_type(value):
    """Sanitize an LLM character_type. Unknown values become 'other'."""
    v = (value or "human").strip().lower()
    return v if v in VALID_CHAR_TYPES else "other"


def coerce_faction_type(value):
    v = (value or "other").strip().lower()
    return v if v in VALID_FACTION_TYPES else "other"


# ── Alias quality ─────────────────────────────────────────────────────────────
# An alias must IDENTIFY a specific individual. Generic forms of address pollute
# the matcher's index and create false merge targets (see README "Aliases must
# be identifying"). These are compared against an already-normalised string, so
# every entry is lower-case.
#
# Expanded after the Phase 0 mention audit, which found generic aliases that
# survived the book-3 cleanup: bare "girl"/"the girl" on Faile, and the junk
# token "What" on Whatley Eldin.

GENERIC_ALIAS_EXACT = {
    # forms of address
    "sister", "sisters", "aes sedai",
    "king", "queen", "the king", "the queen",
    "lord", "lady", "my lord", "my lady",
    "captain", "eldest",
    "mother", "child", "daughter", "son", "my son",
    "the innkeeper",
    # person-noun forms of address (Phase 0 additions)
    "girl", "boy", "lad", "lass", "man", "woman", "wench",
    "the girl", "the boy", "the lad", "the lass", "the man", "the woman",
    "mistress", "stranger", "the stranger", "fellow",
    # junk tokens that are obviously not names (Phase 0 additions)
    "what", "the",
}


def is_generic_alias(text):
    """True if `text` is a generic form of address / junk, not an identifying
    name. Exact (full-string) match against GENERIC_ALIAS_EXACT — substrings
    are NOT flagged, so "Verin Sedai" is spared while "Aes Sedai" is caught."""
    return norm(text) in GENERIC_ALIAS_EXACT


# A curated set of plain English words that occasionally show up as single-token
# aliases by mistake. Used ONLY by looks_like_suspect_alias() to *surface* an
# alias for human review — NOT by the hard is_generic_alias() gate — because
# some real WoT given names collide with common words ("Else", "True"). The
# cleanup tools present these for confirmation rather than auto-deleting them.
SUSPECT_SINGLE_WORDS = frozenset({
    "what", "who", "where", "when", "why", "how", "yes", "no",
    "here", "there", "this", "that", "these", "those",
    "good", "bad", "old", "young", "the", "and", "but",
})


def looks_like_suspect_alias(text):
    """Advisory: True if `text` is a single common-English word that probably
    isn't an identifying name. For surfacing in cleanup reports, not for
    auto-deletion (a human confirms)."""
    n = norm(text)
    return " " not in n and n in SUSPECT_SINGLE_WORDS


# ── Placeholder / collective primary names ────────────────────────────────────
# Lifted verbatim from hygiene_audit._is_b2 / _is_b1 / _is_c so the reconciler's
# write-time gate and the auditor's report classify rows identically.

PRIMARY_NAME_ALLOWLIST = {
    norm(n) for n in [
        "the Creator", "the Dark One", "the Green Man", "the Dragon",
        "the Dragon Reborn", "the Great Lord", "the Great Lord of the Dark",
        "the Eye of the World", "the Forsaken", "the Wheel", "the Pattern",
        "the True Source",
    ]
}

# If the LAST word of a normalised primary_name is one of these, the name is a
# descriptor placeholder for an unnamed walk-on ("the weaselly MAN").
PLACEHOLDER_TAIL_WORDS = {
    "man", "woman", "boy", "girl", "child", "maid", "groom", "wench",
    "wife", "husband", "soldier", "guard", "guardsman", "person", "figure",
    "stranger", "servant", "attendant", "worker", "fellow", "youth", "elder",
}

# If the whole name (after an optional leading "the ") equals one of these
# role-nouns, it is a walk-on placeholder.
ROLE_NOUN_EXACT = {
    "stableman", "innkeeper", "groom", "peddler", "farmwife", "farmhand",
    "serving girl", "serving woman", "serving man", "guardsman", "merchant",
    "farmer", "clerk", "cook", "washerwoman", "herbalist", "seamstress",
    "blacksmith", "fletcher", "armorer", "gleeman", "wisdom", "mayor",
    "stableboy", "shepherd", "captain", "lieutenant", "general",
}


def is_placeholder_name(name):
    """True if a primary_name looks like an extractor-invented placeholder for
    an unnamed walk-on (hygiene_audit Check B2)."""
    nn = norm(name)
    if nn in PRIMARY_NAME_ALLOWLIST:
        return False
    if " with " in nn:                       # "the woman with the dagger"
        return True
    words = nn.split()
    if not words:
        return False
    if words[-1] in PLACEHOLDER_TAIL_WORDS:   # "the weaselly man"
        return True
    bare = nn[4:] if nn.startswith("the ") else nn
    return bare in ROLE_NOUN_EXACT


def is_title_or_group_name(name):
    """True if a primary_name starts with 'the ' and isn't allow-listed or a
    B2 placeholder (hygiene_audit Check B1). Usually a title/group that needs
    the real name found, so the reconciler routes these to review rather than
    deleting them outright."""
    nn = norm(name)
    if nn in PRIMARY_NAME_ALLOWLIST or is_placeholder_name(name):
        return False
    return nn.startswith("the ")


def is_collective_name(name, char_type):
    """True if a creature row's name+type indicate a collective/species label
    rather than a named individual (hygiene_audit Check C)."""
    nn = norm(name)
    if nn in PRIMARY_NAME_ALLOWLIST:
        return False
    if char_type in ("trolloc", "myrddraal"):
        if char_type in nn or (char_type + "s") in nn:
            return True
        if nn.startswith("the ") or nn.startswith("a "):
            return True
    return False


def rejection_reason(name, char_type):
    """Compose the gates for a *new* character. Returns a short reason string if
    the row should NOT be created as-is, else None.

      'placeholder' - unnamed walk-on descriptor (B2): do not create.
      'collective'  - creature collective/species label (C): do not create.
      'title'       - 'the ...' title/group (B1): create-but-review (the real
                      name may appear later; a human confirms or renames).
    """
    if is_placeholder_name(name):
        return "placeholder"
    if is_collective_name(name, char_type):
        return "collective"
    if is_title_or_group_name(name):
        return "title"
    return None


# ── Nationality / origin conventions ──────────────────────────────────────────
# Encodes the README "Nationality conventions": place names not demonyms, no
# hedges/parentheticals, compound hierarchy "Village, Region, Nation", and
# peoples whose identity is their origin kept as the bare group name.

# Placeholder origins are treated as UNRESOLVED so they never block later
# resolution. Mirrors resolve_origins.is_placeholder (startswith "unknown")
# and broadens it.
_ORIGIN_PLACEHOLDER_PREFIXES = ("unknown", "unclear", "unspecified", "n/a",
                                "none", "tbd")


def is_unresolved_origin(value):
    """True if `value` is NULL/empty or a placeholder string. The shared
    definition of 'this origin field is not really filled', used by enrichment
    and by resolve_origins so a literal 'unknown' never counts as filled."""
    if value is None:
        return True
    s = str(value).strip().lower()
    if s == "":
        return True
    return any(s.startswith(p) for p in _ORIGIN_PLACEHOLDER_PREFIXES)


# Demonym -> canonical place name. Place names group; demonyms fragment the
# index ("show me all Andor people" misses everyone labelled "Andoran").
_DEMONYM_TO_PLACE = {
    "andoran": "Andor",
    "cairhienin": "Cairhien",
    "tairen": "Tear",
    "illianer": "Illian",
    "saldaean": "Saldaea",
    "arafellin": "Arafel",
    "shienaran": "Shienar",
    "kandori": "Kandor",
    "ghealdanin": "Ghealdan",
    "murandian": "Murandy",
    "altaran": "Altara",
    "amadician": "Amadicia",
    "taraboner": "Tarabon",
    "domani": "Arad Doman",
    "mayener": "Mayene",
    "malkieri": "Malkier",
    "tar valoner": "Tar Valon",
}

# Peoples whose own name IS their origin — kept as the bare canonical group
# name, never converted. Maps a few common variants to the canonical form.
_PEOPLE_CANONICAL = {
    "aiel": "Aiel",
    "tuatha'an": "Tuatha'an",
    "tinker": "Tuatha'an",
    "tinkers": "Tuatha'an",
    "atha'an miere": "Atha'an Miere",
    "sea folk": "Atha'an Miere",
}


def _normalize_component(part):
    """Normalize one comma-separated component: strip hedges, map demonym ->
    place / people-variant -> canonical. Unknown components pass through with
    surrounding whitespace trimmed."""
    # Drop any parenthetical hedge: "Andor (presumably)" -> "Andor".
    part = re.sub(r"\([^)]*\)", "", part).strip().strip(",").strip()
    if not part:
        return None
    key = norm(part)
    if key in _PEOPLE_CANONICAL:
        return _PEOPLE_CANONICAL[key]
    if key in _DEMONYM_TO_PLACE:
        return _DEMONYM_TO_PLACE[key]
    return part


def normalize_nationality(value):
    """Apply the README nationality conventions. Returns the canonical string,
    or None for a placeholder/empty/hedge-only value (so it is stored as NULL
    and stays eligible for later resolution — this kills the 'unknown' trap)."""
    if is_unresolved_origin(value):
        return None
    parts = [_normalize_component(p) for p in str(value).split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return None
    return ", ".join(parts)


def _origin_keys(canonical):
    """Lower-cased component set of a canonical origin (the compound
    "Village, Region, Nation" pieces)."""
    return {norm(p) for p in canonical.split(",") if p.strip()}


def refine_nationality(current, incoming):
    """Decide what origin to store given a current value and an incoming one.

    Origin is a 'refine toward most specific' trait, not fill-once. Returns
    (value_to_store, is_conflict):

      * current unresolved   -> (normalized incoming, False)   [fill]
      * incoming unresolved  -> (current as-is, False)         [no-op]
      * the two share ANY component (same place at different granularity, e.g.
        "Two Rivers, Andor" vs "Two Rivers" vs "Andor", or a finer
        "Emond's Field, Two Rivers, Andor") -> keep whichever has MORE
        components (more specific); current wins ties.            [refine/keep]
      * no shared component (genuinely different places, "Andor" vs "Tear")
                             -> (normalized current, True)      [CONFLICT]

    Sharing a component is the robust test: coarsening can drop either the
    nation end ("...Andor" -> "Two Rivers") or the region end
    ("Two Rivers, Andor" -> "Andor"), so a positional suffix/prefix check
    misfires; component overlap does not. The conflict flag tells the
    reconciler to keep the current value and queue the divergence for review.
    """
    inc = normalize_nationality(incoming)
    if inc is None:
        return current, False
    cur = normalize_nationality(current)
    if cur is None:
        return inc, False

    ck, ik = _origin_keys(cur), _origin_keys(inc)
    if ck == ik:
        return cur, False
    if ck & ik:                                  # same place, different detail
        return (inc if len(ik) > len(ck) else cur), False
    return cur, True                             # no overlap: real conflict


if __name__ == "__main__":   # pragma: no cover
    # Tiny smoke check so `python scripts/directory_rules.py` is self-verifying.
    assert is_generic_alias("Aes Sedai")
    assert is_generic_alias("the girl")
    assert is_generic_alias("What")
    assert not is_generic_alias("Rand al'Thor")
    assert is_placeholder_name("the weaselly man")
    assert not is_placeholder_name("the Dark One")
    assert normalize_nationality("unknown") is None
    assert normalize_nationality("Andoran") == "Andor"
    assert refine_nationality("Andor",
                              "Emond's Field, Two Rivers, Andor")[0] \
        == "Emond's Field, Two Rivers, Andor"
    print("directory_rules smoke check OK")
