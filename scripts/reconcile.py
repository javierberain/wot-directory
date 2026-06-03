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
        self.entries = []                    # (alias_norm, frozenset(tokens), cid)
        self.aliases_by_cid = defaultdict(list)
        for cid, anorm in conn.execute(
            "SELECT character_id, alias_norm FROM aliases"
        ):
            self.exact[anorm] = cid
            toks = frozenset(anorm.split())
            self.entries.append((anorm, toks, cid))
            self.aliases_by_cid[cid].append(anorm)

    # -- individual strategies --

    def exact_match(self, name):
        return self.exact.get(norm(name))

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


def add_aliases(conn, cid, aliases):
    """Insert aliases, skipping generic forms of address (they pollute the
    matcher index — the delete_aliases.py workload, now prevented at write)."""
    for a in aliases or []:
        text = (a.get("alias_text") or "").strip()
        if not text or is_generic_alias(text):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO aliases "
            "(character_id, alias_text, alias_norm, alias_type, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (cid, text, norm(text),
             a.get("alias_type", "nickname"), a.get("notes")),
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
