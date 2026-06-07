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


# ── Titles, ranks, and name-core extraction ───────────────────────────────────
# Aliases accumulate rank/article decoration ("Lord Agelmar", "Verin Sedai",
# "the High Lady Alteima") and descriptor phrases ("stout little Verin") that are
# not distinct names. They bloat the profile UI and the per-chapter roster sent
# to the model. These helpers strip that decoration so the write-time gate and
# the cleanup can drop redundant variants, and so the matcher can still RESOLVE a
# decorated mention in chapter text to the bare name without STORING the variant.
# Storage norms are NOT changed by any of this; match_key() feeds the matcher and
# the predicates only.

# Rank/honorific phrases, ordered LONGEST FIRST so multiword ranks are removed
# before their single-word components (so "aes sedai" is stripped before a lone
# "sedai" can match).
RANK_TOKENS = [
    "lord captain commander", "lord captain", "high lord", "high lady",
    "aes sedai", "dai shan",
    "lord", "lady", "master", "mistress", "mother", "sedai", "ser", "dame",
]

# Adjectives that, combined with a character's own name, form a DESCRIPTION
# rather than a name ("stout little Verin"). Curated to high-confidence
# physical/age descriptors so the cleanup may auto-drop the epithet.
DESCRIPTOR_ADJECTIVES = {
    "stout", "little", "big", "tall", "short", "old", "young", "fat", "thin",
    "small", "large", "grizzled", "weaselly", "scrawny", "plump", "stocky",
    "lean", "wiry", "burly", "gaunt", "portly",
}


def strip_titles(norm_str):
    """Remove a single leading article and every RANK_TOKENS phrase (longest
    first, on word boundaries) from an ALREADY-NORMALISED string; return the
    name core with whitespace collapsed.

        strip_titles("lord of fal dara")        -> "of fal dara"
        strip_titles("verin mathwin aes sedai") -> "verin mathwin"
        strip_titles("mistress mathwin")        -> "mathwin"

    Storage norms are unaffected; this feeds match_key() and the redundancy
    predicates only.
    """
    s = " " + " ".join((norm_str or "").split()) + " "   # pad for word-boundary
    if s.startswith(" the "):
        s = " " + s[5:]
    for tok in RANK_TOKENS:                               # longest first
        s = s.replace(" " + tok + " ", " ")
    return " ".join(s.split())


def match_key(name):
    """strip_titles(norm(name)) — the rank/article-insensitive key used by the
    matcher ONLY, so 'Verin Sedai' in chapter text resolves to Verin without a
    'verin sedai' alias ever being stored."""
    return strip_titles(norm(name))


def is_rank_decorated_redundant(alias_norm, name_token_set):
    """True if `alias_norm` is just the character's own name wearing rank/article
    decoration: its residue after strip_titles is a non-empty subset of the
    character's name tokens. Catches 'Verin Sedai', 'Lord Agelmar',
    'Mistress Mathwin', 'High Lady Alteima', 'Agelmar Dai Shan'. Spares genuine
    positional/singular titles ('Lord of Fal Dara'), whose residue contains
    words that are not part of the name."""
    residue = set(strip_titles(alias_norm).split())
    return bool(residue) and residue <= set(name_token_set)


def is_descriptor_epithet(alias_norm, name_token_set):
    """True if `alias_norm` is a descriptive phrase built from adjectives plus
    the character's own name ('stout little Verin').

    Tokens (minus the articles "the"/"a") must include >=1 of the character's
    name tokens AND >=1 DESCRIPTOR_ADJECTIVES, and every token must be a name
    token, a descriptor, or an article. Spares 'the Dragon Reborn' (no name
    token) and plain titles (no descriptor)."""
    nts = set(name_token_set)
    tokens = [t for t in norm(alias_norm).split() if t not in ("the", "a")]
    if not tokens:
        return False
    has_name = any(t in nts for t in tokens)
    has_desc = any(t in DESCRIPTOR_ADJECTIVES for t in tokens)
    allowed = nts | DESCRIPTOR_ADJECTIVES | {"the", "a"}
    return has_name and has_desc and all(t in allowed for t in tokens)


# A note that flags the alias as a corrected mistake/misnomer ("mistakenly
# called the Amyrlin", "wrongly named", "not actually her title"): such names
# are not identifying and should never be recorded.
CORRECTED_MISNOMER_RE = re.compile(
    r"(?i)\b(mistak|wrongly|incorrectly|in error|erroneous|corrects?|misname|"
    r"not (actually|really)|confus)")


def is_corrected_misnomer(note):
    """True if a free-text alias note marks the name as a corrected mistake."""
    return bool(note and CORRECTED_MISNOMER_RE.search(note))


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
                                "none", "tbd", "not stated", "not known",
                                "n.a")


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

# Sub-national region -> its immediate parent in the compound hierarchy.
# Used to auto-complete a too-coarse origin to the canonical
# "Village, Region, Nation" form: "Two Rivers" -> "Two Rivers, Andor",
# "Emond's Field" -> "Emond's Field, Two Rivers, Andor". The chain is followed
# until the nation is reached, so the dataset never carries a bare region again.
_REGION_PARENT = {
    "emond's field": "Two Rivers",
    "watch hill": "Two Rivers",
    "taren ferry": "Two Rivers",
    "deven ride": "Two Rivers",
    "two rivers": "Andor",
    "baerlon": "Andor",
    "whitebridge": "Andor",
    "maule": "Tear",
    "lugard": "Murandy",
    "tanchico": "Tarabon",
    "amador": "Amadicia",
    "ebou dar": "Altara",
    "salidar": "Altara",
    "fal dara": "Shienar",
    "fal moran": "Shienar",
    "cachin": "Kandor",
    "maradon": "Saldaea",
    "jehanna": "Ghealdan",
    "caemlyn": "Andor",
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


def _complete_region_tail(parts):
    """Append missing parent regions/nation so a bare/partial region becomes the
    full compound form. "Two Rivers" -> "Two Rivers, Andor"; "Emond's Field" ->
    "Emond's Field, Two Rivers, Andor". No-op once the nation is present."""
    if not parts:
        return parts
    out = list(parts)
    seen = {norm(p) for p in out}
    # follow the chain off the current last (most-general) component
    while True:
        parent = _REGION_PARENT.get(norm(out[-1]))
        if not parent or norm(parent) in seen:
            break
        out.append(parent)
        seen.add(norm(parent))
    return out


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
    parts = _complete_region_tail(parts)
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


# ── Origin taxonomy (deterministic categories) ────────────────────────────────
# The Origin field is broader than geography: it may be a metaphysical or
# Shadow category. Only the categories derivable from local structured data are
# assigned here; Age of Legends (Forsaken etc.) and geographic origins need the
# LLM / text and are NOT guessed.
_TIME_ENTITIES = {"the creator", "the dark one", "machin shin", "mashadar"}


def classify_origin(name, char_type):
    """Return a taxonomy Origin category derivable from name + character_type,
    or None. Used as a write-time fallback when no geographic origin is known:
      - trolloc / myrddraal (Shadowspawn species) -> "Shadow"
      - known cosmic/metaphysical entities         -> "Time"
    Age of Legends and geographic origins are left to the extractor/text."""
    if char_type in ("trolloc", "myrddraal"):
        return "Shadow"
    if norm(name) in _TIME_ENTITIES:
        return "Time"
    return None


# ── Ajah / Black Ajah faction rules ───────────────────────────────────────────
# Normal Ajahs are mutually exclusive (a sister belongs to exactly one public
# Ajah). The Black Ajah is a COVERT, OVERLAPPING faction: membership is additive
# on top of the public Ajah, and it is the ONLY Ajah permitted to coexist with
# another. These helpers let the reconciler enforce that on future imports.

def is_ajah(name, faction_type=None):
    """True if a faction is an Ajah (public or Black)."""
    n = norm(name)
    return faction_type == "ajah" or n == "ajah" or n.endswith(" ajah")


def is_black_ajah(name):
    return norm(name) == "black ajah"


def ajah_conflict(existing_ajah_names, incoming_name):
    """Given the Ajah faction names a character already holds and an incoming
    Ajah name, return True if adding it would violate mutual exclusivity.

    Black Ajah never conflicts (additive); adding a public Ajah conflicts only
    if the character already holds a DIFFERENT public Ajah.
    """
    if is_black_ajah(incoming_name):
        return False
    inc = norm(incoming_name)
    for ex in existing_ajah_names:
        if is_black_ajah(ex):
            continue
        if norm(ex) != inc:
            return True
    return False


if __name__ == "__main__":   # pragma: no cover
    # Tiny smoke check so `python scripts/directory_rules.py` is self-verifying.
    assert is_generic_alias("Aes Sedai")
    assert is_ajah("Green Ajah") and is_black_ajah("Black Ajah")
    assert ajah_conflict(["Green Ajah"], "Blue Ajah")          # two public -> conflict
    assert not ajah_conflict(["Green Ajah"], "Black Ajah")     # black is additive
    assert not ajah_conflict(["Green Ajah", "Black Ajah"], "Green Ajah")
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
    # title / rank / descriptor helpers (real examples from the cleanup task)
    assert strip_titles("lord of fal dara") == "of fal dara"
    assert strip_titles("verin mathwin aes sedai") == "verin mathwin"
    assert strip_titles("mistress mathwin") == "mathwin"
    assert match_key("Verin Sedai") == "verin"
    assert is_rank_decorated_redundant("lord agelmar", {"agelmar", "jagad"})
    assert is_rank_decorated_redundant("agelmar dai shan", {"agelmar", "jagad"})
    assert is_rank_decorated_redundant("high lady alteima", {"alteima"})
    assert is_rank_decorated_redundant("mistress mathwin", {"verin", "mathwin"})
    assert not is_rank_decorated_redundant("lord of fal dara",
                                           {"agelmar", "jagad"})
    assert is_descriptor_epithet("stout little verin", {"verin", "mathwin"})
    assert not is_descriptor_epithet("the dragon reborn", {"rand", "al'thor"})
    assert is_corrected_misnomer("mistakenly called the Amyrlin")
    assert not is_corrected_misnomer("a title she holds")
    print("directory_rules smoke check OK")
