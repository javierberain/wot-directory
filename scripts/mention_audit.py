#!/usr/bin/env python3
"""
mention_audit.py - Read-only audit of the gap between where characters are
*mentioned in the chapter text* and where they have an `appearances` row.

WHY THIS EXISTS
The extraction pipeline writes an `appearances` row only for characters the
LLM judged to be present-and-acting, and `reconcile.py` silently drops the
appearance for any character that failed to resolve (every review-queue
bounce loses its row). The result is that the `appearances` table
systematically under-represents who is actually in a chapter. This tool
quantifies that gap directly from the ground truth — `chapters.full_text` —
the same way `resolve_origins.py` does its gather phase (it deliberately does
not trust the appearances table either).

THIS SCRIPT NEVER MODIFIES THE DATABASE. It opens the DB with SQLite's
mode=ro URI flag so any write attempt raises at the driver level.

It reports two gap types per (character, chapter):

  • MISSING appearance - the character's name/alias occurs in the chapter
    text but there is no appearances row for that (character, chapter).
    Split into:
      - reliable : matched via at least one NON-noisy term (a real name,
                   not a 3-letter or common-English token). High confidence.
      - noisy-only: matched only via short/common terms (e.g. "Lan", "Else",
                   "Red"). Lower confidence — may be a false positive.

  • PHANTOM appearance - an appearances row exists but NONE of the
    character's name terms (nor the row's own name_used) occur anywhere in
    that chapter's full_text. Strong signal of a hallucinated or
    mis-assigned row. Not affected by the noisy-term problem (zero matches).

Usage:
    python scripts/mention_audit.py                       # default db/wot.db
    python scripts/mention_audit.py --db db/wot_book3.db  # a snapshot
    python scripts/mention_audit.py --db db/wot_book3.db --book 3
    python scripts/mention_audit.py --threshold 2         # min matches to flag

Output:
    Per-book summary counts to stdout, plus a detailed CSV at
    data/mention_gaps_<dbstem>.csv (one row per gap).
"""

import argparse
import csv
import os
import pathlib
import re
import sqlite3
import sys

HERE = os.path.dirname(__file__)
DB_PATH = os.path.join(HERE, "..", "db", "wot.db")
DATA_DIR = os.path.join(HERE, "..", "data")

# Minimum number of (reliable) whole-word matches before a character counts as
# "mentioned" in a chapter. 1 is the natural default; raise it to suppress
# one-off passing references.
DEFAULT_THRESHOLD = 1


def norm(name):
    """Lowercase + straight-apostrophe + collapse whitespace. Matches the
    repo-wide norm() used by reconcile.py / hygiene_audit.py."""
    return " ".join((name or "").lower().replace("’", "'").split())


# ── Read-only DB open + noisy-term detection ─────────────────────────────────
# Inlined (rather than imported) from resolve_origins.py so this read-only
# auditor carries no dependency on the API module. Keep the noisy-term logic
# identical to resolve_origins.is_noisy_term so both text-search tools agree
# on what counts as a low-confidence match term.

# Short / common-English tokens that occur as WoT names but also appear in
# ordinary prose, so a whole-word match on them is not trustworthy on its own.
COMMON_ENGLISH_WORDS = frozenset({
    "red", "blue", "green", "brown", "gray", "grey", "white", "black", "gold",
    "else", "true", "first", "last",
    "dark", "light", "good", "glad", "cold", "warm", "calm", "bold",
    "wise", "fair", "free", "high", "long", "deep", "far",
})


def is_noisy_term(term):
    """True if a term is short (<4 chars) or a common English word."""
    t = term.strip()
    return len(t) < 4 or t.lower() in COMMON_ENGLISH_WORDS


def open_db_ro(db_path):
    """Open the DB in strict read-only mode via SQLite's URI interface. Any
    write attempt raises at the driver level, not just by convention."""
    p = pathlib.Path(db_path).resolve()
    if not p.exists():
        sys.exit(f"Database not found: {p}")
    uri = p.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        sys.exit(f"Cannot open database in read-only mode: {exc}\n  {p}")
    conn.row_factory = sqlite3.Row
    return conn


# ── Data loading ──────────────────────────────────────────────────────────────

def load_chapters(conn, book=None):
    """Return chapter rows (with full_text and a cached lowercase copy)."""
    sql = """
        SELECT ch.chapter_id, ch.chapter_number, ch.title,
               ch.full_text, b.series_order, b.title AS book_title
          FROM chapters ch
          JOIN books b ON b.book_id = ch.book_id
    """
    params = ()
    if book is not None:
        sql += " WHERE b.series_order = ?"
        params = (book,)
    sql += " ORDER BY b.series_order, ch.chapter_number"
    chapters = []
    for r in conn.execute(sql, params).fetchall():
        text = r["full_text"] or ""
        chapters.append({
            "chapter_id": r["chapter_id"],
            "chapter_number": r["chapter_number"],
            "title": r["title"],
            "series_order": r["series_order"],
            "book_title": r["book_title"],
            "full_text": text,
            "text_lower": text.lower().replace("’", "'"),
        })
    return chapters


def load_characters(conn):
    """Return {character_id: {primary_name, terms:[...] }} for every character.

    terms is the deduplicated list of primary_name + all alias_text values,
    the same term set resolve_origins.gather_character uses.
    """
    chars = {}
    for r in conn.execute(
        "SELECT character_id, primary_name FROM characters"
    ).fetchall():
        chars[r["character_id"]] = {
            "primary_name": r["primary_name"],
            "terms": [],
            "_seen": set(),
        }
    for r in conn.execute(
        "SELECT character_id, alias_text FROM aliases "
        "ORDER BY is_primary DESC, alias_text"
    ).fetchall():
        c = chars.get(r["character_id"])
        if not c:
            continue
        t = (r["alias_text"] or "").strip()
        if t and t.lower() not in c["_seen"]:
            c["_seen"].add(t.lower())
            c["terms"].append(t)
    # Guard against alias-table gaps: ensure primary_name is a term.
    for c in chars.values():
        pn = (c["primary_name"] or "").strip()
        if pn and pn.lower() not in c["_seen"]:
            c["terms"].insert(0, pn)
            c["_seen"].add(pn.lower())
        del c["_seen"]
    return chars


def load_appearances(conn):
    """Return {(character_id, chapter_id): name_used} for every appearance."""
    out = {}
    for r in conn.execute(
        "SELECT character_id, chapter_id, name_used FROM appearances"
    ).fetchall():
        out[(r["character_id"], r["chapter_id"])] = r["name_used"]
    return out


# ── Matching ──────────────────────────────────────────────────────────────────

_PATTERN_CACHE = {}


def _pattern(term):
    pat = _PATTERN_CACHE.get(term)
    if pat is None:
        pat = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        _PATTERN_CACHE[term] = pat
    return pat


def count_matches(chapter, terms):
    """Return (reliable_count, total_count, matched_terms) for `terms` in one
    chapter. A lowercase substring pre-check short-circuits the regex for the
    overwhelming majority of (term, chapter) pairs that can't match."""
    reliable = 0
    total = 0
    matched = []
    text = chapter["full_text"]
    text_lower = chapter["text_lower"]
    for term in terms:
        # Cheap reject: if the lowercased term isn't even a substring, the
        # whole-word regex cannot match either.
        tl = term.lower().replace("’", "'")
        if tl not in text_lower:
            continue
        n = len(_pattern(term).findall(text))
        if n == 0:
            continue
        total += n
        matched.append(term)
        if not is_noisy_term(term):
            reliable += n
    return reliable, total, matched


# ── Audit ───────────────────────────────────────────────────────────────────--

def run_audit(db_path, book=None, threshold=DEFAULT_THRESHOLD):
    conn = open_db_ro(db_path)
    conn.row_factory = sqlite3.Row

    chapters = load_chapters(conn, book)
    characters = load_characters(conn)
    appearances = load_appearances(conn)
    conn.close()

    if not chapters:
        sys.exit(f"No chapters found in {db_path}"
                 + (f" for book {book}" if book is not None else ""))

    missing_reliable = []   # rows: high-confidence missing appearances
    missing_noisy = []      # rows: low-confidence (noisy-only) missing
    phantom = []            # rows: appearance with zero textual support

    for ch in chapters:
        chid = ch["chapter_id"]
        for cid, c in characters.items():
            reliable, total, matched = count_matches(ch, c["terms"])
            has_app = (cid, chid) in appearances

            if has_app:
                if total == 0:
                    # Phantom: also give the row's own name_used a chance, in
                    # case the text used a name we never stored as an alias.
                    name_used = appearances[(cid, chid)]
                    nu = (name_used or "").lower().replace("’", "'")
                    if nu and nu in ch["text_lower"] and \
                            _pattern(name_used).search(ch["full_text"]):
                        continue  # supported by name_used; not a phantom
                    phantom.append(_row(c, cid, ch, reliable, total,
                                        name_used or ""))
                continue

            # No appearance row — is the character nonetheless mentioned?
            if reliable >= threshold:
                missing_reliable.append(
                    _row(c, cid, ch, reliable, total, ", ".join(matched)))
            elif total >= threshold:
                missing_noisy.append(
                    _row(c, cid, ch, reliable, total, ", ".join(matched)))

    _report(db_path, chapters, characters, appearances,
            missing_reliable, missing_noisy, phantom, threshold)
    _write_csv(db_path, missing_reliable, missing_noisy, phantom)


def _row(c, cid, ch, reliable, total, matched_terms):
    return {
        "character_id": cid,
        "primary_name": c["primary_name"],
        "series_order": ch["series_order"],
        "chapter_number": ch["chapter_number"],
        "chapter_title": ch["title"],
        "reliable_matches": reliable,
        "total_matches": total,
        "matched_terms": matched_terms,
    }


# ── Reporting ─────────────────────────────────────────────────────────────────

_SEP = "=" * 64


def _by_book(rows):
    counts = {}
    for r in rows:
        counts[r["series_order"]] = counts.get(r["series_order"], 0) + 1
    return counts


def _report(db_path, chapters, characters, appearances,
            missing_reliable, missing_noisy, phantom, threshold):
    books = sorted({ch["series_order"] for ch in chapters})
    print()
    print(_SEP)
    print("  MENTION vs APPEARANCE AUDIT")
    print(f"  db: {db_path}")
    print(f"  {len(characters)} characters | {len(chapters)} chapters | "
          f"{len(appearances)} appearance rows | threshold={threshold}")
    print(_SEP)

    mr, mn, ph = _by_book(missing_reliable), _by_book(missing_noisy), \
        _by_book(phantom)
    print(f"\n  {'book':<6}{'missing (reliable)':>20}"
          f"{'missing (noisy)':>18}{'phantom':>10}")
    print("  " + "-" * 52)
    for b in books:
        print(f"  {b:<6}{mr.get(b, 0):>20}{mn.get(b, 0):>18}{ph.get(b, 0):>10}")
    print("  " + "-" * 52)
    print(f"  {'all':<6}{len(missing_reliable):>20}"
          f"{len(missing_noisy):>18}{len(phantom):>10}")

    print("\n  MISSING (reliable): character named in the chapter via a real")
    print("  name but has no appearances row -- the core disparity.")
    print("  PHANTOM: appearances row with no textual support -- likely a")
    print("  hallucinated or mis-assigned row.\n")

    # Show the worst offenders so the numbers are actionable at a glance.
    if phantom:
        print("  Top phantom rows (no name match in chapter text):")
        for r in phantom[:15]:
            print(f"    cid={r['character_id']:>4} "
                  f"\"{r['primary_name']}\"  "
                  f"Bk{r['series_order']}/Ch{r['chapter_number']}")
        if len(phantom) > 15:
            print(f"    ... and {len(phantom) - 15} more (see CSV)")
        print()


def _write_csv(db_path, missing_reliable, missing_noisy, phantom):
    os.makedirs(DATA_DIR, exist_ok=True)
    stem = pathlib.Path(db_path).stem
    out = os.path.join(DATA_DIR, f"mention_gaps_{stem}.csv")
    cols = ["gap_type", "character_id", "primary_name", "series_order",
            "chapter_number", "chapter_title", "reliable_matches",
            "total_matches", "matched_terms"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for gap_type, rows in (("missing_reliable", missing_reliable),
                               ("missing_noisy_only", missing_noisy),
                               ("phantom", phantom)):
            for r in rows:
                w.writerow({"gap_type": gap_type, **r})
    print(f"  CSV written: {out}")
    print()


def main():
    ap = argparse.ArgumentParser(
        description="Read-only audit of the mention-vs-appearance gap.")
    ap.add_argument("--db", default=DB_PATH,
                    help="SQLite DB to audit (default: db/wot.db).")
    ap.add_argument("--book", type=int,
                    help="Restrict to one book's chapters (series_order).")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                    help=f"Min matches to flag a mention (default "
                         f"{DEFAULT_THRESHOLD}).")
    args = ap.parse_args()
    run_audit(args.db, args.book, args.threshold)


if __name__ == "__main__":
    main()
