#!/usr/bin/env python3
"""
reconcile.py - Review an extraction JSON file and commit it to the database.

This is the careful step. The extractor produced raw JSON; this script matches
the extracted characters against the existing roster, decides what to
auto-commit and what to flag for human review, and writes the appearances and
relationships.

Matching strategy, in order (see the Roster class):
  1. Exact match on a known alias (normalized).
  2. Token-subset match — one name's tokens are a subset of a known alias's
     (catches "Byar"->"Jaret Byar", "Else"->"Else Grinwell"). Unambiguous only;
     two candidate characters route to review instead of guessing.
  3. The LLM's own "likely_matches_existing" pointer, verified by similarity.
  4. Genuinely new + passes the write-time gates -> create a new character.
  5. Anything ambiguous / rejected -> parked in review_queue, NOT committed.

What changed from the book 1-3 version (the "fix at origin" work):
  * Validation/normalization is now shared with the auditor via
    directory_rules: placeholder/collective/title names are NOT created (they
    are queued for a human), generic aliases are NOT written, nationality is
    normalized (place-not-demonym, no hedges) and REFINED toward the most
    specific value instead of being filled once and frozen.
  * Appearances are never silently dropped: when a character can't be
    resolved, the review item carries the character's appearance dict and the
    relationships involving it, so resolve_review.py can recover the full data
    without re-reading the extraction JSON by hand.

Usage:
    python reconcile.py --book 1 --chapter 4            # commit/queue
    python reconcile.py --book 1 --chapter 4 --auto     # commit confident items
    python reconcile.py --review                        # list the review queue
"""
import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

from directory_rules import (
    norm,
    coerce_char_type,
    coerce_faction_type,
    is_generic_alias,
    normalize_nationality,
    refine_nationality,
    rejection_reason,
    is_ajah,
    is_black_ajah,
    ajah_conflict,
    classify_origin,
    strip_titles,
    match_key,
    is_rank_decorated_redundant,
    is_descriptor_epithet,
    is_corrected_misnomer,
    RANK_TOKENS,
)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "wot.db")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "extractions")

FUZZY_THRESHOLD = 0.86   # difflib ratio above which we *suggest* (not auto-merge)
POINTER_THRESHOLD = 0.6  # min similarity to trust an LLM pointer

# Tokens that don't distinguish individuals, so a token-subset match resting
# only on them is not trustworthy ("Lord Captain" ⊂ many names). Mirrors the
# spirit of directory_rules generic aliases, at the token level.
TOKEN_STOPWORDS = {
    "the", "a", "an", "of", "and", "my", "son", "daughter", "mother",
    "father", "lord", "lady", "sir", "master", "mistress", "captain",
    "high", "lady's", "lord's", "aes", "sedai",
}


def _distinctive(token):
    """A token worth matching on: not a stopword and not a single letter."""
    return len(token) >= 2 and token not in TOKEN_STOPWORDS


# ── The matcher ───────────────────────────────────────────────────────────────

class Roster:
    """The known-character index, with exact / token-subset / fuzzy matching.

    Pure matching only — no DB writes, no create/queue side effects — so
    backfill_appearances.py can reuse resolve_existing() to add rows against an
    already-cleaned snapshot without risking new creates or merges.
    """

    def __init__(self, conn):
        self.exact = {}                      # alias_norm -> cid
        self.stripped = {}                   # match_key(alias_norm) -> cid|None
        self.entries = []                    # (alias_norm, frozenset(tokens), cid)
        self.aliases_by_cid = defaultdict(list)
        for cid, anorm in conn.execute(
            "SELECT character_id, alias_norm FROM aliases"
        ):
            self.exact[anorm] = cid
            toks = frozenset(anorm.split())
            self.entries.append((anorm, toks, cid))
            self.aliases_by_cid[cid].append(anorm)
            # Secondary rank/article-insensitive index so a decorated mention in
            # chapter text ('Verin Sedai') resolves to the bare name without a
            # 'verin sedai' alias being stored. A key shared by >1 character is
            # marked ambiguous (None) so it can never mis-match.
            key = match_key(anorm)
            if key:
                if key not in self.stripped:
                    self.stripped[key] = cid
                elif self.stripped[key] != cid:
                    self.stripped[key] = None

    # -- individual strategies --

    def exact_match(self, name):
        cid = self.exact.get(norm(name))
        if cid is not None:
            return cid
        # Fall back to the rank/article-stripped key (ignoring ambiguous None).
        key = match_key(name)
        if key:
            scid = self.stripped.get(key)
            if scid is not None:
                return scid
        return None

    def token_candidates(self, name):
        """{cid: (distinctive_shared_count, score)} for known names in a
        token-subset relationship with `name`.

        A SINGLE shared distinctive token (a bare surname or given name) is NOT
        a reliable match in either direction: "Dannil Lewin" vs the ancestral
        "Lewin", or "Adan" vs "Governor Adan", are different people who merely
        share a name. resolve_existing() therefore only auto-accepts when >=2
        distinctive tokens are shared, or when the LLM pointer corroborates a
        single-token candidate. The count is returned so it can make that call.
        """
        toks = frozenset(norm(name).split())
        if not toks:
            return {}
        cands = {}
        for _anorm, atoks, cid in self.entries:
            if not atoks:
                continue
            if toks <= atoks or atoks <= toks:
                shared = toks & atoks
                d = sum(1 for t in shared if _distinctive(t))
                if d == 0:
                    continue
                score = len(shared) / max(len(toks), len(atoks))
                pd, ps = cands.get(cid, (0, 0.0))
                cands[cid] = (max(pd, d), max(ps, score))
        return cands

    def fuzzy(self, name):
        """(cid, score) of the best difflib match over all aliases."""
        n = norm(name)
        best_id, best = None, 0.0
        for anorm, _toks, cid in self.entries:
            r = difflib.SequenceMatcher(None, n, anorm).ratio()
            if r > best:
                best_id, best = cid, r
        return best_id, best

    def pointer_similarity(self, name, cid):
        n = norm(name)
        return max(
            (difflib.SequenceMatcher(None, n, a).ratio()
             for a in self.aliases_by_cid.get(cid, [])),
            default=0.0,
        )

    # -- composed, side-effect-free resolution to an EXISTING row --

    def resolve_existing(self, name, pointer=None):
        """Resolve `name` to an existing character_id without creating anything.

        Returns (cid, method, candidates):
          ("exact"/"token_subset"/"llm_pointer", cid)  - confident match
          (None, "ambiguous", {cid: score, ...})       - >1 token candidate
          (None, "none", None)                         - no confident match
        """
        cid = self.exact_match(name)
        if cid is not None:
            return cid, "exact", None

        cands = self.token_candidates(name)   # cid -> (distinctive_shared, score)
        strong = {c: v for c, v in cands.items() if v[0] >= 2}
        weak = {c: v for c, v in cands.items() if v[0] == 1}

        # >=2 shared distinctive tokens is a confident structural match.
        if len(strong) == 1:
            return next(iter(strong)), "token_subset", None
        if len(strong) > 1:
            return None, "ambiguous", {c: v[1] for c, v in strong.items()}

        # A single shared token only counts if the LLM pointer corroborates it.
        if pointer:
            pcid = self.exact_match(pointer)
            if pcid is not None and (
                    pcid in cands
                    or self.pointer_similarity(name, pcid) >= POINTER_THRESHOLD):
                return pcid, "llm_pointer", None

        # Several different characters share the one token -> human decides.
        if len(weak) > 1:
            return None, "ambiguous", {c: v[1] for c, v in weak.items()}

        # A lone single-token candidate with no pointer is not trusted: fall
        # through so an is_new character is created separately rather than
        # silently folded into a namesake.
        return None, "none", None


# ── Write helpers ─────────────────────────────────────────────────────────────

def get_primary_name(conn, cid):
    r = conn.execute(
        "SELECT primary_name FROM characters WHERE character_id = ?", (cid,)
    ).fetchone()
    return r[0] if r else None


def _has_column(conn, table, column):
    return any(r[1] == column
               for r in conn.execute(f"PRAGMA table_info({table})"))


# ── Canonical-name promotion (primary_name = fullest known proper name) ────────
# primary_name is set once at create and never promoted to a fuller name learned
# later (so 'Agelmar' never became 'Agelmar Jagad'). These helpers promote the
# primary to the fullest proper name among the character's own names — extending
# it, never replacing it with a different name (superset guard).

_NAME_ARTICLES = {"the", "a", "an"}


def _strip_leading_rank(text):
    """Remove a single leading article ('the') and/or a single leading
    RANK_TOKENS phrase (longest first) from the ORIGINAL-cased `text`, preserving
    the casing/spelling of the remaining tokens."""
    toks = text.split()
    low = [norm(t) for t in toks]
    if low and low[0] == "the":
        toks, low = toks[1:], low[1:]
    for phrase in RANK_TOKENS:                      # already longest-first
        pl = phrase.split()
        if pl and low[:len(pl)] == pl:
            toks, low = toks[len(pl):], low[len(pl):]
            break
    return " ".join(toks)


def canonical_adopt_text(text):
    """Clean a name for adoption as primary_name.

    Two transforms, on the ORIGINAL-cased text (so casing/spelling survive):
      1. strip a leading rank/article prefix ('High Lady Suroth Sabelle
         Meldarath' -> 'Suroth Sabelle Meldarath'),
      2. collapse the ceremonial connective ' of House ' to a single space
         ('Elayne of House Trakand' -> 'Elayne Trakand').
    Aiel ' of the ...' and Ogier 'son of ...' forms are left untouched.
    """
    s = re.sub(r"\s+of\s+house\s+", " ", " " + text.strip() + " ",
               flags=re.IGNORECASE).strip()
    s = _strip_leading_rank(s)
    return " ".join(s.split())


def _distinctive_name_tokens(name_norm):
    """Tokens of a name's core: rank/article decoration stripped, articles
    dropped. Used to score how 'full' a candidate name is."""
    return [t for t in strip_titles(name_norm).split() if t not in _NAME_ARTICLES]


def _adopt_tokens(text):
    """Distinctive tokens of `text` after canonical adoption-cleaning — the basis
    for scoring fullness and for the superset guard, so 'of House' / leading rank
    are not counted as distinguishing tokens."""
    return set(_distinctive_name_tokens(norm(canonical_adopt_text(text))))


def choose_canonical_name(candidates):
    """Pick the fullest proper name among (text, norm) candidates and return its
    CLEANED canonical form (ready to adopt as primary_name).

    candidates[0] MUST be the current primary (text, norm); the rest are
    alternative names (given_name aliases, and — in the cleanup only — a
    differing display_name). Each is scored by its number of distinctive name
    tokens AFTER adoption-cleaning (leading rank/article stripped, ' of House '
    collapsed). The winner is the unique highest-scoring candidate whose cleaned
    token set is a SUPERSET of the current primary's cleaned tokens (it extends
    the primary, never replaces it); its CLEANED text is returned. On a tie among
    extensions, or if nothing beats the current primary, returns the current
    primary's text unchanged. Side-effect-free; None only for an empty list.
    """
    if not candidates:
        return None
    cur_text = candidates[0][0]
    cur_clean_norm = norm(canonical_adopt_text(cur_text))
    cur_tokens = _adopt_tokens(cur_text)
    cur_score = len(cur_tokens)
    # Key by CLEANED norm so 'High Lady X' and 'the X' don't count as a tie, and
    # so the same name from given_name + display_name collapses to one.
    by_norm = {}
    for text, _anorm in candidates[1:]:
        clean = canonical_adopt_text(text)
        cnorm = norm(clean)
        if not cnorm or cnorm == cur_clean_norm or cnorm in by_norm:
            continue
        toks = _adopt_tokens(text)
        if len(toks) > cur_score and cur_tokens <= toks:
            by_norm[cnorm] = (len(toks), clean)
    if not by_norm:
        return cur_text
    top = max(score for score, _ in by_norm.values())
    winners = [clean for score, clean in by_norm.values() if score == top]
    return winners[0] if len(winners) == 1 else cur_text


def promotion_candidates(conn, cid):
    """The (text, norm) candidates for choose_canonical_name, current primary
    FIRST: the current primary, then given_name aliases, then a differing legacy
    display_name (only present on a not-yet-migrated snapshot). Shared by
    apply_promotion and the alias-cleanup so both score names identically."""
    cur = conn.execute(
        "SELECT primary_name FROM characters WHERE character_id = ?", (cid,)
    ).fetchone()
    if not cur:
        return []
    cur_primary = cur[0]
    candidates = [(cur_primary, norm(cur_primary))]
    for (text,) in conn.execute(
        "SELECT alias_text FROM aliases WHERE character_id = ? "
        "AND alias_type = 'given_name'", (cid,)
    ):
        candidates.append((text, norm(text)))
    if _has_column(conn, "characters", "display_name"):
        row = conn.execute(
            "SELECT display_name FROM characters WHERE character_id = ?", (cid,)
        ).fetchone()
        dn = (row[0] or "").strip() if row else ""
        if dn and norm(dn) != norm(cur_primary):
            candidates.append((dn, norm(dn)))
    return candidates


def apply_promotion(conn, cid):
    """Promote a character's primary_name to the fullest known proper name.

    Gathers the current primary + given_name aliases (and, when the legacy
    display_name column still exists, a differing display_name) as candidates,
    runs choose_canonical_name, and — if a fuller name wins AND no other
    character already owns that primary_name — flips the chosen alias to
    'primary' and demotes the old primary to 'given_name'. If another character
    already has the chosen primary_name, this is a duplicate-character case:
    queue it for review and do NOT promote. Returns the new primary_name if
    promoted, else None. Exactly one is_primary=1 alias remains.
    """
    candidates = promotion_candidates(conn, cid)
    if not candidates:
        return None
    cur_primary = candidates[0][0]

    chosen = choose_canonical_name(candidates)
    if not chosen or norm(chosen) == norm(cur_primary):
        return None

    dup = conn.execute(
        "SELECT character_id FROM characters "
        "WHERE primary_name = ? AND character_id != ?", (chosen, cid)
    ).fetchone()
    if dup:
        conn.execute(
            "INSERT INTO review_queue (chapter_id, kind, payload, note) "
            "VALUES (NULL, 'promotion_blocked_duplicate', ?, ?)",
            (json.dumps({"character_id": cid, "from": cur_primary,
                         "to": chosen, "conflicts_with": dup[0]}),
             f"Cannot promote '{cur_primary}' -> '{chosen}': character_id "
             f"{dup[0]} already has that primary_name."),
        )
        return None

    chosen_norm = norm(chosen)
    # Any decorated candidate whose CLEANED form is the adopted name (e.g.
    # 'High Lady Suroth Sabelle Meldarath' -> 'Suroth Sabelle Meldarath',
    # 'Elayne of House Trakand' -> 'Elayne Trakand') stays as a title alias
    # rather than being lost when we adopt the tightened primary.
    for text, anorm in candidates:
        if anorm != chosen_norm and norm(canonical_adopt_text(text)) == chosen_norm:
            conn.execute(
                "UPDATE aliases SET alias_type = 'title', is_primary = 0 "
                "WHERE character_id = ? AND alias_norm = ?", (cid, anorm),
            )
    # Ensure the chosen exists as an alias row (it may be a newly-cleaned form,
    # or have come from display_name).
    conn.execute(
        "INSERT OR IGNORE INTO aliases "
        "(character_id, alias_text, alias_norm, alias_type, is_primary) "
        "VALUES (?, ?, ?, 'given_name', 0)",
        (cid, chosen, chosen_norm),
    )
    # Demote the old primary, promote the chosen.
    conn.execute(
        "UPDATE aliases SET alias_type = 'given_name', is_primary = 0 "
        "WHERE character_id = ? AND alias_norm = ?", (cid, norm(cur_primary)),
    )
    conn.execute(
        "UPDATE aliases SET alias_type = 'primary', is_primary = 1 "
        "WHERE character_id = ? AND alias_norm = ?", (cid, chosen_norm),
    )
    conn.execute(
        "UPDATE characters SET primary_name = ?, updated_at = datetime('now') "
        "WHERE character_id = ?", (chosen, cid),
    )
    n_primary = conn.execute(
        "SELECT COUNT(*) FROM aliases WHERE character_id = ? AND is_primary = 1",
        (cid,)).fetchone()[0]
    assert n_primary == 1, \
        f"character {cid} has {n_primary} primary aliases after promotion"
    return chosen


# Alias types that always represent identity and are never auto-dropped.
PROTECTED_ALIAS_TYPES = ("primary", "given_name", "disguise")
# Alias types subject to the quality gate / cleanup rules.
DROPPABLE_ALIAS_TYPES = ("title", "epithet", "nickname")


def name_token_set(conn, cid):
    """The character's identity tokens — the rank/article-stripped tokens of its
    primary + given_name aliases. A rank-decorated or descriptor alias is
    redundant exactly when its residue is a subset of this set."""
    toks = set()
    for (anorm,) in conn.execute(
        "SELECT alias_norm FROM aliases WHERE character_id = ? "
        "AND alias_type IN ('primary', 'given_name')", (cid,)
    ):
        toks |= set(strip_titles(anorm).split())
    return toks


def add_aliases(conn, cid, aliases):
    """Insert aliases, applying the write-time quality gate so the alias index
    and the per-chapter roster stay clean.

    PROTECTED types (primary, given_name, disguise) are always inserted (minus
    exact-generic junk). DROPPABLE types (title, epithet, nickname) are skipped
    when rank-decorated redundant, descriptor epithets, or corrected misnomers.
    Article-only duplicates of a title/epithet collapse to the 'the'-prefixed
    form, symmetrically regardless of insert order.
    """
    nts = name_token_set(conn, cid)
    for a in aliases or []:
        text = (a.get("alias_text") or "").strip()
        if not text or is_generic_alias(text):
            continue
        atype = a.get("alias_type", "nickname")
        notes = a.get("notes")
        n = norm(text)

        if atype not in PROTECTED_ALIAS_TYPES:
            if is_corrected_misnomer(notes):
                continue
            if is_rank_decorated_redundant(n, nts):
                continue
            if is_descriptor_epithet(n, nts):
                continue
            # Article-only duplicate: keep the "the"-prefixed form.
            if atype in ("title", "epithet"):
                if not n.startswith("the "):
                    if conn.execute(
                        "SELECT 1 FROM aliases WHERE character_id = ? "
                        "AND alias_norm = ?", (cid, "the " + n)
                    ).fetchone():
                        continue                       # 'the X' exists; drop bare
                else:
                    conn.execute(                      # drop a bare 'X' if present
                        "DELETE FROM aliases WHERE character_id = ? "
                        "AND alias_norm = ? AND alias_type IN ('title','epithet')",
                        (cid, n[4:]),
                    )

        conn.execute(
            "INSERT OR IGNORE INTO aliases "
            "(character_id, alias_text, alias_norm, alias_type, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (cid, text, n, atype, notes),
        )


def create_character(conn, char):
    """Insert a new character + its aliases. Returns the new character_id.
    Nationality is normalized on the way in, so a placeholder becomes NULL
    (never the literal 'unknown') and a demonym becomes a place name."""
    st = char.get("stable_traits", {}) or {}
    primary = char.get("name_used_in_text", "").strip()
    ctype = coerce_char_type(char.get("character_type"))
    # Normalized geographic origin if known; else a deterministic taxonomy
    # category (Shadow for Shadowspawn, Time for cosmic entities); else NULL.
    origin = normalize_nationality(st.get("nationality")) \
        or classify_origin(primary, ctype)
    cur = conn.execute(
        """INSERT INTO characters
           (primary_name, character_type, nationality, physical_traits, age,
            filiations, personality)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (primary, ctype, origin,
         st.get("physical_traits"), st.get("age"), st.get("filiations"),
         st.get("personality")),
    )
    cid = cur.lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO aliases "
        "(character_id, alias_text, alias_norm, alias_type, is_primary) "
        "VALUES (?, ?, ?, 'primary', 1)",
        (cid, primary, norm(primary)),
    )
    add_aliases(conn, cid, char.get("aliases_observed", []))
    apply_promotion(conn, cid)
    return cid


def enrich_character(conn, cid, char, chapter_id):
    """Fill empty stable traits; REFINE nationality (coarse->fine, conflict to
    review) instead of the old fill-once COALESCE. Returns a list of review
    payloads to queue (origin conflicts), so the caller owns all queueing."""
    st = char.get("stable_traits", {}) or {}
    reviews = []

    # nationality: refine toward the most specific value
    incoming_nat = st.get("nationality")
    if incoming_nat:
        cur_nat = conn.execute(
            "SELECT nationality FROM characters WHERE character_id = ?", (cid,)
        ).fetchone()[0]
        new_nat, conflict = refine_nationality(cur_nat, incoming_nat)
        if conflict:
            reviews.append((
                "origin_conflict",
                f"Origin conflict on '{get_primary_name(conn, cid)}': have "
                f"'{cur_nat}', chapter suggests "
                f"'{normalize_nationality(incoming_nat)}'. Kept existing.",
            ))
        elif new_nat != cur_nat:
            conn.execute(
                "UPDATE characters SET nationality = ?, "
                "updated_at = datetime('now') WHERE character_id = ?",
                (new_nat, cid),
            )

    # other stable traits keep fill-once-if-empty semantics
    for col in ("physical_traits", "age", "filiations", "personality"):
        val = st.get(col)
        if val:
            conn.execute(
                f"UPDATE characters SET {col} = COALESCE({col}, ?), "
                f"updated_at = datetime('now') WHERE character_id = ?",
                (val, cid),
            )

    add_aliases(conn, cid, char.get("aliases_observed", []))
    # A fuller proper name may have arrived this chapter; promote the primary
    # (enrich previously never touched primary_name).
    apply_promotion(conn, cid)
    return reviews


def reconcile_factions(conn, cid, char, chapter_id):
    """Match each LLM-reported faction by name, create new ones, write joins.
    Idempotent: re-running a chapter does not duplicate rows."""
    for fac in char.get("factions", []) or []:
        name = (fac.get("name") or "").strip()
        if not name:
            continue
        nnorm = norm(name)
        ftype = coerce_faction_type(fac.get("faction_type"))

        # Ajah mutual-exclusivity: normal Ajahs don't overlap; Black Ajah is the
        # only Ajah allowed on top of a public one (additive). If this character
        # already holds a different public Ajah, skip the conflicting incoming
        # one rather than recording an impossible "Green + Blue".
        if is_ajah(name, ftype) and not is_black_ajah(name):
            existing = [r[0] for r in conn.execute(
                "SELECT f.name FROM character_factions cf "
                "JOIN factions f ON f.faction_id = cf.faction_id "
                "WHERE cf.character_id = ? AND f.faction_type = 'ajah'", (cid,))]
            if ajah_conflict(existing, name):
                print(f"  [skip] Ajah conflict: '{name}' not added to "
                      f"{get_primary_name(conn, cid)} (already in {existing})")
                continue

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
        conn.execute(
            "INSERT OR IGNORE INTO character_factions "
            "(character_id, faction_id, role, first_chapter_id, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (cid, fid, role, chapter_id, fac.get("notes")),
        )
        if role == "leader":
            conn.execute(
                "UPDATE character_factions SET role = 'leader' "
                "WHERE character_id = ? AND faction_id = ?",
                (cid, fid),
            )


# ── Review queue (now carries appearance + relationships) ─────────────────────

def _appearance_for(data, name):
    nn = norm(name)
    for a in data.get("appearances", []):
        if norm(a.get("character")) == nn:
            return a
    return None


def _relationships_for(data, name):
    nn = norm(name)
    return [r for r in data.get("relationships", [])
            if norm(r.get("character_a")) == nn
            or norm(r.get("character_b")) == nn]


def queue_review(conn, chapter_id, kind, char, note, data):
    """Park an item in the review queue with a SELF-RECOVERING payload: the
    character dict PLUS its appearance dict and the relationships involving it.
    resolve_review.py replays these on resolution, so no appearance is lost and
    no per-book cleanup script needs to re-read the extraction JSON."""
    name = (char.get("name_used_in_text") or "").strip()
    payload = {
        "character": char,
        "appearance": _appearance_for(data, name),
        "relationships": _relationships_for(data, name),
    }
    conn.execute(
        "INSERT INTO review_queue (chapter_id, kind, payload, note) "
        "VALUES (?, ?, ?, ?)",
        (chapter_id, kind, json.dumps(payload, ensure_ascii=False), note),
    )


# ── Per-character resolution ──────────────────────────────────────────────────

def resolve_one(conn, roster, char, data, chapter_id, auto):
    """Resolve one extracted character to a cid, creating/queueing as needed.
    Returns the cid if the character is committed (existing or newly created),
    or None if it was parked in review."""
    name = char.get("name_used_in_text", "").strip()
    if not name:
        return None
    conf = char.get("confidence", "low")
    pointer = char.get("likely_matches_existing")

    cid, method, cands = roster.resolve_existing(name, pointer)

    # Ambiguous token overlap only blocks when the LLM thinks the character is
    # EXISTING (we just can't tell which). If the LLM says it's NEW, the shared
    # token is coincidental (a new "Adine Lewin" vs the ancestral "Lewin") — fall
    # through and create it rather than parking a brand-new character in review.
    if method == "ambiguous" and not char.get("is_new_character"):
        names = ", ".join(
            f"{get_primary_name(conn, c)} ({s:.0%})" for c, s in cands.items())
        note = f"'{name}' token-matches multiple characters: {names}."
        print(f"  [REVIEW] ambiguous_character: {note}")
        queue_review(conn, chapter_id, "ambiguous_character", char, note, data)
        return None

    # An LLM pointer that pointed somewhere but failed the similarity check is
    # surfaced distinctly (suspicious_llm_match), matching the old behavior.
    if cid is None and pointer and not char.get("is_new_character"):
        pcid = roster.exact_match(pointer)
        if pcid is not None:
            score = roster.pointer_similarity(name, pcid)
            note = (f"LLM pointer rejected: '{name}' -> "
                    f"'{get_primary_name(conn, pcid)}' but similarity "
                    f"{score:.0%} < {POINTER_THRESHOLD:.0%}.")
            print(f"  [REVIEW] suspicious_llm_match: {note}")
            queue_review(conn, chapter_id, "suspicious_llm_match",
                         char, note, data)
            return None

    if cid is not None:
        print(f"  [{method}] '{name}' -> {get_primary_name(conn, cid)}")
        for kind, note in enrich_character(conn, cid, char, chapter_id):
            print(f"  [REVIEW] {kind}: {note}")
            queue_review(conn, chapter_id, kind, char, note, data)
        return cid

    # ── not matched to any existing row ──────────────────────────────────────
    if char.get("is_new_character"):
        reason = rejection_reason(name, coerce_char_type(
            char.get("character_type")))
        if reason is not None:
            kind = f"rejected_{reason}"
            note = (f"'{name}' looks like a {reason} name, not an individual; "
                    f"not created. Supply the proper name or dismiss.")
            print(f"  [REVIEW] {kind}: {note}")
            queue_review(conn, chapter_id, kind, char, note, data)
            return None

        fid, score = roster.fuzzy(name)
        if fid and score >= FUZZY_THRESHOLD:
            note = (f"LLM says new, but '{name}' is {score:.0%} similar to "
                    f"'{get_primary_name(conn, fid)}'.")
            if auto and conf == "high":
                cid = create_character(conn, char)
                print(f"  [new*] '{name}' created (despite {score:.0%} "
                      f"similarity - review later)")
                queue_review(conn, chapter_id, "possible_duplicate",
                             char, note, data)
                return cid
            print(f"  [REVIEW] possible_duplicate: {note}")
            queue_review(conn, chapter_id, "possible_duplicate",
                         char, note, data)
            return None

        cid = create_character(conn, char)
        print(f"  [new] '{name}' created as a new character")
        return cid

    # claimed not-new but unresolved
    fid, score = roster.fuzzy(name)
    suggestion = (f" closest: '{get_primary_name(conn, fid)}' ({score:.0%})"
                  if fid else "")
    print(f"  [REVIEW] ambiguous_character: '{name}' unresolved.{suggestion}")
    queue_review(conn, chapter_id, "ambiguous_character", char,
                 f"Unresolved.{suggestion}", data)
    return None


# ── Main reconcile ────────────────────────────────────────────────────────────

def reconcile(book_order, chapter_number, auto=False, db_path=None):
    path = os.path.join(OUT_DIR, f"b{book_order}_c{chapter_number}.json")
    if not os.path.exists(path):
        sys.exit(f"No extraction file at {path}. Run extract_chapter.py first.")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    chapter_id = data["_meta"]["chapter_id"]
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    roster = Roster(conn)

    resolved = {}   # name_used_in_text -> cid

    print(f"\n=== Reconciling book {book_order} chapter {chapter_number}: "
          f"{data['_meta']['chapter_title']} ===\n")

    for char in data.get("characters", []):
        cid = resolve_one(conn, roster, char, data, chapter_id, auto)
        if cid is None:
            continue
        name = char.get("name_used_in_text", "").strip()
        resolved[name] = cid
        reconcile_factions(conn, cid, char, chapter_id)
        roster = Roster(conn)   # refresh so later chars see new rows/aliases

    # ----- appearances ----- (carried in review payloads when unresolved)
    app_count = 0
    dropped_apps = 0
    for app in data.get("appearances", []):
        cid = resolved.get((app.get("character") or "").strip())
        if cid is None:
            dropped_apps += 1
            continue
        alliances = app.get("alliances_shown") or []
        conn.execute(
            """INSERT OR REPLACE INTO appearances
               (character_id, chapter_id, name_used, whereabouts,
                notable_actions, alliances_shown, demeanor)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cid, chapter_id, app.get("character"), app.get("whereabouts"),
             app.get("notable_actions"),
             ", ".join(alliances) if alliances else None, app.get("demeanor")),
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
        a = resolved.get((rel.get("character_a") or "").strip())
        b = resolved.get((rel.get("character_b") or "").strip())
        if a is None or b is None or a == b:
            continue
        lo, hi = sorted((a, b))
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

    # ----- mentions ----- (referenced-but-absent characters)
    # Resolve ONLY against rows that exist (roster) or were resolved/created in
    # this chapter. Never create a character from a mere mention.
    mention_count = 0
    for m in data.get("mentions", []):
        name = (m.get("character") or "").strip()
        if not name:
            continue
        cid = resolved.get(name)
        if cid is None:
            cid, method, _ = roster.resolve_existing(name)
            if cid is None:
                continue   # unknown character only mentioned in passing — skip
        # Don't double-record: a character present this chapter (has an
        # appearance) is not also a mention.
        if conn.execute(
            "SELECT 1 FROM appearances WHERE character_id = ? AND chapter_id = ?",
            (cid, chapter_id)).fetchone():
            continue
        conn.execute(
            "INSERT OR IGNORE INTO mentions "
            "(character_id, chapter_id, name_used, context) VALUES (?, ?, ?, ?)",
            (cid, chapter_id, name, m.get("context")))
        mention_count += 1

    conn.execute("UPDATE chapters SET extracted = 1 WHERE chapter_id = ?",
                 (chapter_id,))
    conn.commit()

    pending = conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE resolved = 0"
    ).fetchone()[0]
    print(f"\nCommitted: {app_count} appearances, {mention_count} mentions, "
          f"{rel_count} relationships.")
    if dropped_apps:
        print(f"{dropped_apps} appearance(s) belong to unresolved characters "
              f"-- carried in their review-queue payloads, not lost.")
    print(f"Review queue now holds {pending} unresolved item(s).")
    if pending:
        print("Run  python reconcile.py --review  to see them.")
    conn.close()


def show_review_queue(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH)
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
    print("Resolve with  python reconcile.py --review  then "
          "scripts/resolve_review.py, or edit the database directly.")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", type=int, help="series order")
    ap.add_argument("--chapter", type=int, help="chapter number")
    ap.add_argument("--auto", action="store_true",
                    help="auto-commit high-confidence items")
    ap.add_argument("--review", action="store_true",
                    help="list the review queue and exit")
    ap.add_argument("--db", help="target database (default: db/wot.db)")
    args = ap.parse_args()

    if args.review:
        show_review_queue(db_path=args.db)
        return
    if args.book is None or args.chapter is None:
        sys.exit("Provide --book and --chapter, or use --review.")
    reconcile(args.book, args.chapter, args.auto, db_path=args.db)


if __name__ == "__main__":
    main()
