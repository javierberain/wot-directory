#!/usr/bin/env python3
"""
resolve_origins.py - Two-phase origin resolver for the WoT character directory.

Many characters have a placeholder `nationality` ('unknown', 'unknown (not Two
Rivers)', NULL, etc.) because the original extraction never captured their origin
even when the book text states it.  This script re-derives nationality from the
actual chapter text via targeted Claude API calls.

Phase 1 — GATHER (free, no API)
    Searches every chapter's full_text for each character's name terms using
    whole-word regex matching.  Collects surrounding passages (~200-char window).
    Writes a reviewable bundle to data/origins_gather_<dbname>.json.
    Prints a summary table; flags characters whose terms include short (<4 char)
    or common-English-word tokens that may produce noisy results.

Phase 2 — RESOLVE (API, dry-run by default)
    Sends gathered passages to Claude and asks it to derive nationality ONLY from
    the supplied text.  Verifies that the returned evidence phrase actually appears
    in the source chapter text (not just the windowed passages — guards against
    fabricated citations and windowing false negatives).  Dry-run by default;
    use --commit to write to the database.  Never overwrites an existing
    non-placeholder nationality.

Usage:
    # Two-phase workflow (review before committing):
    python scripts/resolve_origins.py --db PATH --ids 26,138,163
        → dry-run: calls API, prints results, writes proposals JSON
    python scripts/resolve_origins.py --db PATH --commit-from-proposals data/origins_proposals_<db>.json
        → applies proposals without a second API call

    # One-shot workflow (gather + resolve + write in one step):
    python scripts/resolve_origins.py --db PATH --ids 26,138,163 --commit

    # Gather only (no API):
    python scripts/resolve_origins.py --db PATH --ids 26,138,163 --gather-only

    --db defaults to db/wot.db when omitted.
    --book SERIES_ORDER restricts the gather to a single book's chapters
        (e.g. --book 3 limits to The Dragon Reborn's text only).  Without
        this flag, all chapters in the database are searched.  Useful for
        per-book DBs where prior books' text has already been resolved and
        re-running on them would just re-derive previously-considered
        evidence.
    --gather-only stops after Phase 1 (no API calls, no DB writes).
    --commit writes resolved nationalities to the database (dry-run otherwise).
    --commit-from-proposals PATH reads a saved dry-run proposals file and
        applies it without calling the API; requires --db.
    --allow-db-mismatch overrides the db_file validation in --commit-from-proposals.
    --commit and --commit-from-proposals are mutually exclusive.
    --commit and --gather-only are mutually exclusive.

Safety envelope
    • Dry-run by default; --commit required to write.
    • Backup written next to the --db file (<db>.pre-origins.bak, with WAL/SHM
      sidecars) before the first write.  No backup is written in dry-run or
      gather-only mode.
    • Only fills placeholder/NULL nationality; never overwrites a real value.
      The re-check happens inside the commit transaction, not just at gather time.
    • Reads chapters.full_text only.  Never reads data/extractions/*.json.
    • Does NOT use the appearances table; presence is determined by text search.
    • Book-agnostic: no hardcoded book numbers or character IDs.
"""

import argparse
import csv
import json
import os
import pathlib
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()


# ── Paths ─────────────────────────────────────────────────────────────────────
# Default DB follows the same convention as every other script in this repo.
DB_PATH  = os.path.join(os.path.dirname(__file__), "..", "db", "wot.db")
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data",
                         "origins_resolved.csv")

# Model: same string as extract_chapter.py and hygiene_audit.py.
MODEL = "claude-sonnet-4-5"

# Proposals file schema version — increment if the schema changes.
SCRIPT_VERSION = "1.0"

# Passage context window (characters on each side of a regex match).
PASSAGE_WINDOW = 200


# ── Placeholder detection ─────────────────────────────────────────────────────
# A nationality is a placeholder if it is NULL, empty, or starts with "unknown"
# (case-insensitive).  This covers:
#   NULL, "", "unknown", "Unknown", "unknown (not Two Rivers)", etc.
# Any other value (e.g. "Andoran", "Two Rivers", "Malkieri") is real and must
# never be overwritten.

def is_placeholder(nationality):
    """Return True if nationality is NULL, empty, or an 'unknown' placeholder."""
    if nationality is None or str(nationality).strip() == "":
        return True
    return str(nationality).strip().lower().startswith("unknown")


# ── Noisy-term detection ──────────────────────────────────────────────────────
# A search term is flagged as POSSIBLY-NOISY when it is very short or a common
# English word, because whole-word regex matching may still produce spurious
# results (e.g. "Else" matching "if else", "Red" in ordinary sentences).
#
# Short threshold: < 4 characters (strict, as specified).
# Common-English set: curated for terms that occur as WoT character names AND
# appear naturally in English prose.

COMMON_ENGLISH_WORDS = frozenset({
    # Colours / Ajah names used as aliases
    "red", "blue", "green", "brown", "gray", "grey", "white", "black", "gold",
    # Common conjunctions / adjectives that appear as character short-names
    "else", "true", "first", "last",
    # Other short common words seen in WoT aliases
    "dark", "light", "good", "glad", "cold", "warm", "calm", "bold",
    "wise", "fair", "free", "high", "long", "deep", "far",
})


def is_noisy_term(term):
    """Return True if the term is short (<4 chars) or a common English word."""
    t = term.strip()
    if len(t) < 4:                       # e.g. "Lan" (3), "Jak" (3), "Red" (3)
        return True
    if t.lower() in COMMON_ENGLISH_WORDS:  # e.g. "Else", "True", "Gold"
        return True
    return False


# ── Evidence normalisation ────────────────────────────────────────────────────

def norm_for_search(text):
    """Normalise text for evidence verification against source chapter text.

    Applies three transformations so that a model-returned evidence phrase
    matches its source even when minor typographic differences exist:

      1. Lowercase — makes the check case-insensitive.
      2. Curly/smart apostrophes → straight ASCII apostrophe (U+0027).
         Mirrors the apostrophe handling in norm() used elsewhere in the repo.
         EPUB text commonly uses U+2019 (right single quotation mark) where
         the model may return a plain apostrophe, or vice versa.
      3. Collapse whitespace — any run of spaces, newlines, tabs, or carriage
         returns becomes a single space.  This is the key fix: a long evidence
         sentence that spans a passage-window boundary may have been split
         across two clipped snippets in the passages block, but in the original
         full_text it is one continuous run of characters separated only by
         ordinary whitespace.

    Used by verify_evidence() on both the evidence phrase and the source
    chapter full_text before the substring check.
    """
    text = text.lower()
    text = text.replace('’', "'").replace('‘', "'")  # smart → straight
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ── Passage extraction ────────────────────────────────────────────────────────

def extract_passage(text, match_start, match_end, window=PASSAGE_WINDOW):
    """Return up to `window` characters of context on each side of a match.

    Clips to the nearest whitespace boundary so the returned snippet never
    starts or ends mid-word.  The matched span itself is always included.
    """
    raw_start = max(0, match_start - window)
    raw_end   = min(len(text), match_end + window)

    # Advance start right to the next whitespace so we don't open mid-word.
    start = raw_start
    if start > 0:
        while start < match_start and text[start] not in " \n\t\r":
            start += 1

    # Retreat end left to the previous whitespace so we don't close mid-word.
    end = raw_end
    if end < len(text):
        while end > match_end and text[end] not in " \n\t\r":
            end -= 1

    return text[start:end].strip()


# ── Database helpers ──────────────────────────────────────────────────────────

def open_db_ro(db_path):
    """Open the database in strict read-only mode via SQLite's URI interface.

    Any write attempt raises OperationalError at the driver level — enforced
    by SQLite itself, not just by convention.
    """
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


def open_db_rw(db_path):
    """Open the database read-write with foreign-key enforcement on."""
    p = pathlib.Path(db_path).resolve()
    if not p.exists():
        sys.exit(f"Database not found: {p}")
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def take_backup(db_path):
    """Copy the database to <db_path>.pre-origins.bak, with WAL/SHM sidecars."""
    p   = pathlib.Path(db_path).resolve()
    bak = pathlib.Path(str(p) + ".pre-origins.bak")
    if not p.exists():
        sys.exit(f"Database not found: {p}")
    shutil.copy2(p, bak)
    # Copy WAL/SHM sidecar files if they exist (preserves in-flight state).
    for ext in ("-wal", "-shm"):
        sidecar = pathlib.Path(str(p) + ext)
        if sidecar.exists():
            shutil.copy2(sidecar, pathlib.Path(str(bak) + ext))
            print(f"  (also backed up WAL sidecar {sidecar.name})")
    print(f"Backup written: {bak}")


# ── Phase 1: GATHER ───────────────────────────────────────────────────────────

def gather_character(conn, cid, all_chapters):
    """Gather text passages mentioning character `cid` from all chapter full_texts.

    Searches every chapter's full_text using whole-word, case-insensitive regex
    for each of the character's name terms (primary_name + all alias_text values).
    Does NOT consult the appearances table; presence is determined solely by text.

    `all_chapters` is the pre-fetched list of chapter rows (as sqlite3.Row objects)
    from run_gather — passing it in avoids re-querying the full chapter table for
    every character.

    Returns a dict suitable for inclusion in the gather bundle JSON, or a dict
    with an "error" key if the character_id is not found.
    """
    char = conn.execute(
        "SELECT character_id, primary_name, nationality "
        "FROM characters WHERE character_id = ?",
        (cid,),
    ).fetchone()
    if not char:
        return {"error": f"character_id={cid} not found in database"}

    # ── Collect search terms ──────────────────────────────────────────────────
    # Use primary_name + all alias_text values.
    # Do NOT use display_name — it is empty in this dataset.
    alias_rows = conn.execute(
        "SELECT alias_text, alias_type "
        "FROM aliases WHERE character_id = ? "
        "ORDER BY is_primary DESC, alias_type, alias_text",
        (cid,),
    ).fetchall()

    # Deduplicated term list, primary alias first.
    seen  = set()
    terms = []   # list of {"text": str, "alias_type": str}
    for row in alias_rows:
        t = row["alias_text"].strip()
        if t and t not in seen:
            seen.add(t)
            terms.append({"text": t, "alias_type": row["alias_type"]})

    # Ensure primary_name is present (guard against alias table gaps).
    pn = char["primary_name"].strip()
    if pn and pn not in seen:
        terms.insert(0, {"text": pn, "alias_type": "primary_name"})
        seen.add(pn)

    noisy_terms = [t["text"] for t in terms if is_noisy_term(t["text"])]

    # ── Search all chapter full_texts ─────────────────────────────────────────
    # `all_chapters` was pre-fetched by run_gather; reuse it here so the full
    # chapter table is read only once per script invocation, not once per
    # character.
    matches      = []
    match_counts = {t["text"]: 0 for t in terms}

    for ch in all_chapters:
        full_text = ch["full_text"] or ""
        for term_info in terms:
            term = term_info["text"]
            # ── THE WHOLE-WORD REGEX ──────────────────────────────────────────
            # re.escape() makes special characters (apostrophes, hyphens, etc.)
            # literal.  \b word boundaries prevent "Lan" matching "island" or
            # "plan".  IGNORECASE allows "lan", "LAN", "Lan" to all match.
            pattern = re.compile(
                r"\b" + re.escape(term) + r"\b",
                re.IGNORECASE,
            )
            for m in pattern.finditer(full_text):
                match_counts[term] += 1
                passage = extract_passage(full_text, m.start(), m.end())
                matches.append({
                    "term":           term,
                    "chapter_id":     ch["chapter_id"],
                    "series_order":   ch["series_order"],
                    "chapter_number": ch["chapter_number"],
                    "chapter_title":  ch["title"],
                    "book_title":     ch["book_title"],
                    "passage":        passage,
                })

    distinct_chapter_ids = sorted({m["chapter_id"] for m in matches})

    return {
        "character_id":            cid,
        "primary_name":            char["primary_name"],
        "current_nationality":     char["nationality"],
        "is_placeholder":          is_placeholder(char["nationality"]),
        "terms":                   [t["text"] for t in terms],
        "term_alias_types":        {t["text"]: t["alias_type"] for t in terms},
        "noisy_terms":             noisy_terms,
        "match_counts":            match_counts,
        "total_matches":           len(matches),
        "distinct_chapter_count":  len(distinct_chapter_ids),
        "distinct_chapter_ids":    distinct_chapter_ids,
        "matches":                 matches,
    }


# Formatting helpers
_SEP  = "-" * 60
_SEP2 = "=" * 60


def run_gather(conn, ids, db_path, book_series_order=None):
    """Phase 1: gather passages for all requested character_ids.

    Fetches all chapters once up front (avoids a per-character full-table scan),
    then calls gather_character for each requested id.

    If `book_series_order` is supplied, only chapters whose books.series_order
    matches are searched.  This is the right behaviour for per-book DBs where
    earlier-book text has already been processed in a prior resolver pass —
    re-sending it to the API just reproduces previous (often-rejected)
    proposals.  When omitted, every chapter in the DB is searched (the
    original behaviour).

    Writes the bundle to data/origins_gather_<dbstem>.json and prints a
    summary table to stdout.  Returns (bundle, chapter_texts) where
    chapter_texts is a {chapter_id: full_text} mapping used by Phase 2's
    verify_evidence to check against the original source text rather than the
    windowed passages block.
    """
    db_stem  = pathlib.Path(db_path).stem
    out_path = pathlib.Path(DATA_DIR) / f"origins_gather_{db_stem}.json"
    pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

    # ── Validate book_series_order if supplied ────────────────────────────────
    book_scope_label = "all books"
    if book_series_order is not None:
        book_row = conn.execute(
            "SELECT book_id, title FROM books WHERE series_order = ?",
            (book_series_order,),
        ).fetchone()
        if not book_row:
            sys.exit(
                f"ERROR: --book {book_series_order} not found in this database. "
                f"Available books: " +
                ", ".join(
                    f"{r['series_order']} ({r['title']!r})"
                    for r in conn.execute(
                        "SELECT series_order, title FROM books ORDER BY series_order"
                    ).fetchall()
                )
            )
        book_scope_label = f"Bk{book_series_order} {book_row['title']!r} only"

    print()
    print(_SEP2)
    print("  PHASE 1 — GATHER")
    print(f"  {len(ids)} character(s)  |  scope: {book_scope_label}")
    print(f"  Database: {pathlib.Path(db_path).resolve()}")
    print(_SEP2)
    print()

    # ── Pre-fetch chapters once (respecting --book filter if supplied) ────────
    # Reused by every gather_character call and retained for verify_evidence.
    if book_series_order is None:
        all_chapters = conn.execute(
            "SELECT ch.chapter_id, ch.chapter_number, ch.title, ch.full_text, "
            "       b.series_order, b.title AS book_title "
            "FROM chapters ch "
            "JOIN books b ON b.book_id = ch.book_id "
            "ORDER BY b.series_order, ch.chapter_number",
        ).fetchall()
    else:
        all_chapters = conn.execute(
            "SELECT ch.chapter_id, ch.chapter_number, ch.title, ch.full_text, "
            "       b.series_order, b.title AS book_title "
            "FROM chapters ch "
            "JOIN books b ON b.book_id = ch.book_id "
            "WHERE b.series_order = ? "
            "ORDER BY b.series_order, ch.chapter_number",
            (book_series_order,),
        ).fetchall()

    # Build the source-text map used by verify_evidence in Phase 2.
    chapter_texts = {
        ch["chapter_id"]: ch["full_text"] or ""
        for ch in all_chapters
    }

    bundle = {
        "db":           str(pathlib.Path(db_path).resolve()),
        "db_stem":      db_stem,
        "generated":    datetime.now(timezone.utc).isoformat(),
        "book_scope":   book_series_order,
        "scope_label":  book_scope_label,
        "characters":   {},
    }

    gathered = []
    for cid in ids:
        print(f"  Gathering character_id={cid} ...", end="", flush=True)
        result = gather_character(conn, cid, all_chapters)
        if "error" in result:
            print(f"\n  ERROR: {result['error']}")
            continue
        bundle["characters"][str(cid)] = result
        noisy_flag = (f"  [POSSIBLY-NOISY: {result['noisy_terms']}]"
                      if result["noisy_terms"] else "")
        skip_flag  = ("  [NOT-PLACEHOLDER — will skip in resolve]"
                      if not result["is_placeholder"] else "")
        print(f"  {result['total_matches']} matches, "
              f"{result['distinct_chapter_count']} chapter(s)"
              f"{noisy_flag}{skip_flag}")
        gathered.append(result)

    # ── Summary table ─────────────────────────────────────────────────────────
    print()
    print(_SEP)
    print(f"  {'ID':>6}  {'Name':<30}  {'Matches':>7}  {'Chs':>4}  Flags")
    print(_SEP)
    for r in gathered:
        flags = []
        if r["noisy_terms"]:
            flags.append("NOISY:" + ",".join(r["noisy_terms"]))
        if not r["is_placeholder"]:
            flags.append("NOT-PLACEHOLDER")
        if r["total_matches"] == 0:
            flags.append("NO-PASSAGES")
        print(f"  {r['character_id']:>6}  {r['primary_name']:<30}  "
              f"{r['total_matches']:>7}  {r['distinct_chapter_count']:>4}  "
              + ("  ".join(flags) if flags else "—"))
    print(_SEP)
    print()

    # ── Write bundle ──────────────────────────────────────────────────────────
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2, ensure_ascii=False)
    print(f"Gather bundle written: {out_path}")
    print()

    return bundle, chapter_texts


# ── Phase 2: RESOLVE ──────────────────────────────────────────────────────────

# The resolve prompt.  Double-braces {{ }} are literal braces in the final
# string (needed because the JSON format instruction uses single braces).
_RESOLVE_PROMPT = """\
You are a research assistant helping to fill in the nationality / homeland field
for characters in a Wheel of Time character directory.

CHARACTER
  primary_name      : {primary_name}
  known titles / epithets : {titles}

GATHERED PASSAGES
These are every occurrence of this character's names in the book text.
{passages_block}

YOUR TASK
Using ONLY the passages above, determine this character's nationality, people,
or homeland.  You may assign an origin ONLY when the passages contain:
  - An explicit statement of homeland or people
    ("he was Malkieri", "the Andoran woman", "born in Shienar", etc.)
  - A title that directly names a homeland or people
    ("crownless King of the Malkieri" => Malkieri,
     "Defender of the Dragonwall" => Cairhienin, etc.)

You must NOT infer origin from:
  - Faction or organisation membership (being a Forsaken, a Whitecloak, an
    Aes Sedai, a Warder, etc. does not establish a homeland)
  - An office, rank, or role ("Captain-General", "Amyrlin", "Lord", etc.)
  - Merely being present in, travelling through, or ruling a location

Special case — Ogier characters: their origin is their STEDDING
(e.g. "Stedding Shangtai"), never the bare word "Ogier" (that is their
species, not their homeland).  If the stedding is not stated in the passages,
return not_stated.

If none of the allowed bases apply, you MUST return nationality=null,
basis="not_stated".

Rules you must follow:
  - The "evidence" field must be an EXACT VERBATIM substring copied
    character-for-character directly from the passages above.  Do NOT
    paraphrase, reconstruct, or summarise.  If you cannot find a verbatim
    phrase that supports the origin, you MUST return not_stated instead.
  - Do NOT use any knowledge from outside the supplied passages.
  - Keep the nationality label SHORT — 1 to 4 words
    (e.g. "Malkieri", "Two Rivers", "Andoran", "Aiel", "Cairhienin",
     "Stedding Shangtai").

Return ONLY a single JSON object with exactly these three keys — no prose, no
markdown fences:
{{"nationality": "<short label or null>", "evidence": "<exact verbatim phrase, or empty string>", "basis": "explicit|title|not_stated"}}
"""


def build_passages_block(char_data):
    """Format gathered matches into a text block suitable for the API prompt.

    Groups passages by chapter (sorted), deduplicates identical passage strings
    within the same chapter, and labels each group with book/chapter metadata.
    """
    if not char_data["matches"]:
        return "(no passages found)"

    # Group by (series_order, chapter_number, chapter_title).
    by_chapter = {}
    for m in char_data["matches"]:
        key = (m["series_order"], m["chapter_number"], m["chapter_title"])
        by_chapter.setdefault(key, [])
        if m["passage"] not in by_chapter[key]:   # deduplicate within chapter
            by_chapter[key].append(m["passage"])

    lines = []
    for (order, num, title), passages in sorted(by_chapter.items()):
        lines.append(f"[Bk{order} Ch{num}: {title}]")
        for p in passages:
            lines.append(f"  {p}")
        lines.append("")

    return "\n".join(lines)


def verify_evidence(evidence, matched_chapter_ids, chapter_texts):
    """Return True if the evidence phrase appears in any of the matched chapter texts.

    Checks the normalised evidence phrase (via norm_for_search) against the
    normalised source full_text for every chapter that matched this character
    during gather.  Checking the source text rather than the windowed passages
    block avoids false negatives caused by a long evidence sentence spanning a
    passage-window boundary — the phrase is always contiguous in the original.

    An empty evidence string always passes (expected for not_stated results).
    Returns False only when the phrase cannot be found anywhere in the matched
    chapters, which is a reliable signal of a fabricated citation.
    """
    if not evidence or not evidence.strip():
        return True
    norm_ev = norm_for_search(evidence)
    for cid in matched_chapter_ids:
        source = chapter_texts.get(cid, "")
        if norm_ev in norm_for_search(source):
            return True
    return False


def call_api(primary_name, title_aliases, passages_block):
    """Call Claude to derive nationality from the gathered passages.

    Returns the parsed JSON dict {"nationality", "evidence", "basis"}.
    Strips markdown code fences if the model wraps its output in them.
    Raises on JSON parse error or API error — callers should catch.
    """
    try:
        import anthropic
    except ImportError:
        sys.exit("ERROR: anthropic package not installed (pip install anthropic).")

    titles_str = ", ".join(title_aliases) if title_aliases else "(none)"
    prompt = _RESOLVE_PROMPT.format(
        primary_name=primary_name,
        titles=titles_str,
        passages_block=passages_block,
    )

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()

    # Strip markdown code fences if present (model sometimes wraps output).
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$",          "", raw.rstrip())

    # Extract the FIRST top-level JSON object from the response.
    # The model sometimes appends an explanation or a second object after the
    # JSON (common with noisy/ambiguous names that generate many passages).
    # json.loads() on the full string then fails with "Extra data".  A
    # brace-depth scan finds the matching closing brace for the first '{' and
    # parses only that substring, making the extraction tolerant of any
    # trailing text.
    first_brace = raw.find("{")
    if first_brace == -1:
        raise ValueError(
            f"Model response contains no JSON object.  Raw response: {raw!r}"
        )
    depth = 0
    for i, ch in enumerate(raw[first_brace:], start=first_brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(raw[first_brace : i + 1])
    raise ValueError(
        f"Model response contains an unclosed JSON object.  Raw response: {raw!r}"
    )


def run_resolve(bundle, chapter_texts, db_path, dry_run=True):
    """Phase 2: call the API for each character and report proposed nationalities.

    dry_run=True  (default) — print proposed changes only; no DB writes.
    dry_run=False           — take backup, write to DB in a single transaction.

    `chapter_texts` is the {chapter_id: full_text} map returned by run_gather.
    It is passed to verify_evidence so that the fabrication guard checks the
    evidence phrase against the original source text rather than the windowed
    passages block (which cannot reliably contain long cross-boundary phrases).

    For each character:
      - Skip if current nationality is not a placeholder.
      - Skip API call if no passages were gathered (record as not_stated).
      - Verify the evidence phrase appears in the source chapter text; downgrade
        to not_stated on failure.
      - In --commit mode, re-check the DB value inside the transaction before
        writing (guards against the value changing between gather and commit).
    """
    mode_label = "DRY-RUN" if dry_run else "COMMIT"
    print()
    print(_SEP2)
    print(f"  PHASE 2 — RESOLVE  [{mode_label}]")
    print(f"  Model: {MODEL}")
    print(_SEP2)
    print()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ERROR: ANTHROPIC_API_KEY is not set.\n"
            "Make sure it is defined in .env or exported in the environment."
        )

    results = []   # accumulated per-character outcome dicts

    for cid_str, char_data in bundle["characters"].items():
        cid = int(cid_str)
        pn  = char_data["primary_name"]
        cur = char_data["current_nationality"]

        print(f"  character_id={cid}  \"{pn}\"")
        print(f"    current nationality : {cur!r}")

        # ── Skip non-placeholders immediately ─────────────────────────────────
        if not char_data["is_placeholder"]:
            print("    → KEPT EXISTING  (nationality is not a placeholder)")
            print()
            results.append({
                "character_id":      cid,  "primary_name":       pn,
                "nationality":       cur,  "basis":              "—",
                "evidence":          "",   "action":             "kept_existing",
                "error_msg":         None, "downgraded":         False,
                "current_nationality": cur,
            })
            continue

        # ── No passages → not_stated without an API call ──────────────────────
        if char_data["total_matches"] == 0:
            print("    → NO PASSAGES FOUND  (recording as not_stated; no API call)")
            print()
            results.append({
                "character_id":      cid,  "primary_name":       pn,
                "nationality":       None, "basis":              "not_stated",
                "evidence":          "",   "action":             "no_passages",
                "error_msg":         None, "downgraded":         False,
                "current_nationality": cur,
            })
            continue

        # ── Build prompt inputs ───────────────────────────────────────────────
        passages_block = build_passages_block(char_data)
        title_aliases  = [
            t for t, atype in char_data["term_alias_types"].items()
            if atype in ("title", "epithet")
        ]

        print(f"    passages  : {char_data['total_matches']} matches "
              f"across {char_data['distinct_chapter_count']} chapter(s)")
        if char_data["noisy_terms"]:
            print(f"    [POSSIBLY-NOISY terms: {char_data['noisy_terms']}]")
        print("    Calling API ...", end="", flush=True)

        # ── API call ──────────────────────────────────────────────────────────
        try:
            api_result = call_api(pn, title_aliases, passages_block)
        except Exception as exc:
            err_msg = str(exc)
            print(f"\n    ERROR: {err_msg}")
            print()
            # Record the failure explicitly so the character appears in the
            # summary as ERROR rather than silently vanishing.  Errored
            # characters are never written to the DB or the CSV.
            results.append({
                "character_id":      cid,      "primary_name":       pn,
                "nationality":       None,     "basis":              "—",
                "evidence":          "",       "action":             "error",
                "error_msg":         err_msg,  "downgraded":         False,
                "current_nationality": cur,
            })
            continue

        nationality = api_result.get("nationality")
        evidence    = api_result.get("evidence", "")
        basis       = api_result.get("basis", "not_stated")

        # ── Evidence verification ─────────────────────────────────────────────
        # Guards against the model fabricating a citation not in the source text.
        downgraded = False
        if nationality and not verify_evidence(
            evidence, char_data["distinct_chapter_ids"], chapter_texts
        ):
            downgraded  = True
            print(f"\n    DOWNGRADED: evidence phrase not found in source chapter "
                  f"text (fabrication guard)")
            print(f"    Returned evidence was: {evidence!r}")
            nationality = None
            basis       = "not_stated"
            evidence    = ""
        else:
            print("  done")

        # ── Report ────────────────────────────────────────────────────────────
        print(f"    proposed nationality: {nationality!r}")
        print(f"    basis     : {basis}")
        if evidence:
            print(f"    evidence  : \"{evidence}\"")

        action = "would_write" if nationality else "not_stated"
        results.append({
            "character_id":      cid,        "primary_name":       pn,
            "nationality":       nationality, "basis":              basis,
            "evidence":          evidence,    "action":             action,
            "error_msg":         None,        "downgraded":         downgraded,
            "current_nationality": cur,
        })
        print()

    # ── Commit (only when --commit was passed) ────────────────────────────────
    if not dry_run:
        writable = [r for r in results if r["nationality"]
                    and r["action"] == "would_write"]

        if not writable:
            print("Nothing to write — all results are not_stated or kept_existing.")
        else:
            take_backup(db_path)
            print()
            conn = open_db_rw(db_path)
            try:
                written = 0
                for r in writable:
                    # Re-read inside the transaction: the value may have changed
                    # since gather (e.g. a concurrent manual edit).
                    row = conn.execute(
                        "SELECT nationality FROM characters "
                        "WHERE character_id = ?",
                        (r["character_id"],),
                    ).fetchone()
                    if row is None:
                        print(f"  WARN: character_id={r['character_id']} "
                              f"not found in DB — skipped")
                        r["action"] = "not_found"
                        continue
                    if not is_placeholder(row["nationality"]):
                        print(f"  WARN: character_id={r['character_id']} "
                              f"\"{r['primary_name']}\" — nationality is now "
                              f"{row['nationality']!r} (no longer a placeholder) "
                              f"— kept existing")
                        r["action"] = "kept_existing"
                        continue
                    conn.execute(
                        "UPDATE characters SET nationality = ? "
                        "WHERE character_id = ?",
                        (r["nationality"], r["character_id"]),
                    )
                    old = row["nationality"]
                    print(f"  WRITTEN: character_id={r['character_id']}  "
                          f"\"{r['primary_name']}\"  "
                          f"{old!r} → {r['nationality']!r}")
                    r["action"] = "write"
                    written += 1
                conn.commit()
            except Exception as exc:
                conn.rollback()
                conn.close()
                sys.exit(
                    f"\nERROR during write: {exc}\n"
                    f"All changes rolled back.  Database is unchanged."
                )
            conn.close()
            print(f"\n{written} nationality value(s) written.")

            # Append committed results to the cross-book CSV record.
            committed = [r for r in results if r["action"] == "write"]
            if committed:
                append_resolved_csv(db_path, committed)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print(_SEP)
    print("  RESOLVE SUMMARY")
    print(_SEP)
    print(f"  {'ID':>6}  {'Name':<30}  {'Nationality':<22}  {'Basis':<15}  Action")
    print(_SEP)
    for r in results:
        # ERROR rows: show blank nationality so they stand out from not_stated.
        if r["action"] == "error":
            nat_label = ""
        else:
            nat_label = r["nationality"] or "(not_stated)"
        print(f"  {r['character_id']:>6}  {r['primary_name']:<30}  "
              f"{nat_label:<22}  {r['basis']:<15}  {r['action']}")
    print(_SEP)
    print()
    if dry_run:
        print("  Dry-run complete.  Re-run with --commit to write to the database.")
        print()
        write_proposals(db_path, results)

    return results


# ── Cross-book CSV record ─────────────────────────────────────────────────────

_CSV_HEADERS = [
    "db_file", "character_id", "primary_name",
    "nationality", "basis", "evidence",
]


def append_resolved_csv(db_path, results):
    """Append committed results to data/origins_resolved.csv.

    This is the cross-book record; the script only ever appends — no other
    logic.  The header row is written only when the file is first created.
    Each row carries the absolute path of the source database so entries from
    different books are distinguishable.
    """
    csv_out = pathlib.Path(CSV_PATH).resolve()
    pathlib.Path(csv_out.parent).mkdir(parents=True, exist_ok=True)
    write_header = not csv_out.exists()
    db_label     = str(pathlib.Path(db_path).resolve())

    with open(csv_out, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_HEADERS)
        if write_header:
            writer.writeheader()
        for r in results:
            writer.writerow({
                "db_file":      db_label,
                "character_id": r["character_id"],
                "primary_name": r["primary_name"],
                "nationality":  r["nationality"] or "",
                "basis":        r["basis"],
                "evidence":     r["evidence"],
            })
    print(f"CSV record appended: {csv_out}")


# ── Proposal persistence ──────────────────────────────────────────────────────

def write_proposals(db_path, results):
    """Write the dry-run result set to data/origins_proposals_<dbstem>.json.

    Called at the end of every dry-run so the exact API responses can be
    reviewed and then applied via --commit-from-proposals without a second
    API call.  Overwrites any prior proposals file for this database.

    Schema (schema_version=1):
      {
        "schema_version": 1,
        "generated_at_iso": "<ISO 8601 UTC>",
        "db_file": "<absolute path used during dry-run>",
        "script_version": "<SCRIPT_VERSION constant>",
        "results": [ { per-character result dict }, ... ]
      }

    Each result dict carries: character_id, primary_name, nationality, basis,
    evidence, action, error_msg, downgraded, current_nationality.
    """
    db_stem  = pathlib.Path(db_path).stem
    out_path = pathlib.Path(DATA_DIR) / f"origins_proposals_{db_stem}.json"
    pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version":   1,
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "db_file":          str(pathlib.Path(db_path).resolve()),
        "script_version":   SCRIPT_VERSION,
        "results": [
            {
                "character_id":       r["character_id"],
                "primary_name":       r["primary_name"],
                "nationality":        r["nationality"],
                "basis":              r["basis"],
                "evidence":           r["evidence"],
                "action":             r["action"],
                "error_msg":          r.get("error_msg"),
                "downgraded":         r.get("downgraded", False),
                "current_nationality": r.get("current_nationality"),
            }
            for r in results
        ],
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"  Proposals written to: {out_path}")
    return out_path


def commit_from_proposals(proposals_path, db_path, allow_db_mismatch=False):
    """Apply a saved dry-run proposals file to the target database.

    Does NOT call the API.  For each result with action=='would_write',
    re-reads the CURRENT nationality from the target database (the JSON's
    current_nationality is a snapshot from dry-run time and must not be
    trusted), then writes only if the value is still a placeholder.

    Appends written results to data/origins_resolved.csv, same as the
    existing --commit path.
    """
    # ── Read and validate proposals file ──────────────────────────────────────
    p = pathlib.Path(proposals_path).resolve()
    if not p.exists():
        sys.exit(f"\nERROR: proposals file not found: {p}")

    try:
        with open(p, "r", encoding="utf-8") as f:
            proposals = json.load(f)
    except json.JSONDecodeError as exc:
        sys.exit(f"\nERROR: proposals file is not valid JSON: {exc}")

    schema_version = proposals.get("schema_version")
    if schema_version != 1:
        sys.exit(
            f"\nERROR: unrecognised proposals schema_version={schema_version!r}.  "
            f"This script supports only version 1."
        )

    # ── db_file validation ────────────────────────────────────────────────────
    proposals_db = proposals.get("db_file", "")
    target_db    = str(pathlib.Path(db_path).resolve())
    if proposals_db != target_db:
        mismatch_detail = (
            f"  proposals db_file : {proposals_db}\n"
            f"  --db target       : {target_db}"
        )
        if not allow_db_mismatch:
            sys.exit(
                f"\nERROR: db_file in proposals does not match --db.\n"
                f"{mismatch_detail}\n"
                f"  Pass --allow-db-mismatch to override (e.g. if the database\n"
                f"  was moved or renamed since the dry-run was run)."
            )
        print(f"WARNING: db_file mismatch (--allow-db-mismatch set):\n"
              f"{mismatch_detail}")

    # ── Filter to writable results ────────────────────────────────────────────
    all_results = proposals.get("results", [])
    writable    = [r for r in all_results if r.get("action") == "would_write"]
    generated   = proposals.get("generated_at_iso", "(unknown)")

    print()
    print(_SEP2)
    print("  PHASE 2 — COMMIT FROM PROPOSALS  (no API calls)")
    print(f"  Proposals file : {p}")
    print(f"  Generated at   : {generated}")
    print(f"  Total results  : {len(all_results)}")
    print(f"  Would-write    : {len(writable)}")
    print(f"  Target db      : {target_db}")
    print(_SEP2)
    print()

    if not writable:
        print("  Nothing to write — no would_write entries in proposals.")
        return

    # ── Backup + write ────────────────────────────────────────────────────────
    take_backup(db_path)
    print()

    conn          = open_db_rw(db_path)
    written_results = []
    try:
        written = 0
        for r in writable:
            cid = r["character_id"]
            pn  = r["primary_name"]
            nat = r["nationality"]

            # Re-read the CURRENT nationality — the JSON snapshot is stale.
            row = conn.execute(
                "SELECT nationality FROM characters WHERE character_id = ?",
                (cid,),
            ).fetchone()

            if row is None:
                print(f"  WARN: character_id={cid}  \"{pn}\"  "
                      f"not found in DB — skipped")
                continue

            if not is_placeholder(row["nationality"]):
                print(f"  ALREADY HAS REAL VALUE: character_id={cid}  "
                      f"\"{pn}\"  current={row['nationality']!r}  "
                      f"proposed={nat!r}  — skipped")
                continue

            old = row["nationality"]
            conn.execute(
                "UPDATE characters SET nationality = ? WHERE character_id = ?",
                (nat, cid),
            )
            print(f"  WRITTEN: character_id={cid}  \"{pn}\"  "
                  f"{old!r}  →  {nat!r}")
            written_results.append(dict(r, action="write"))
            written += 1

        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        sys.exit(
            f"\nERROR during write: {exc}\n"
            f"All changes rolled back.  Database is unchanged."
        )
    conn.close()

    print(f"\n  {written} nationality value(s) written.")

    # ── Append to cross-book CSV record ───────────────────────────────────────
    if written_results:
        append_resolved_csv(db_path, written_results)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print(_SEP)
    print("  COMMIT-FROM-PROPOSALS SUMMARY")
    print(_SEP)
    print(f"  {'ID':>6}  {'Name':<30}  {'Nationality':<22}  {'Basis':<15}  Action")
    print(_SEP)
    written_ids = {r["character_id"] for r in written_results}
    for r in writable:
        nat_label    = r["nationality"] or "(not_stated)"
        action_label = "write" if r["character_id"] in written_ids else "skipped"
        print(f"  {r['character_id']:>6}  {r['primary_name']:<30}  "
              f"{nat_label:<22}  {r['basis']:<15}  {action_label}")
    print(_SEP)
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Two-phase origin resolver for the WoT character directory.",
    )
    ap.add_argument(
        "--db", metavar="PATH",
        help="Path to the SQLite database file to operate on. "
             "Defaults to db/wot.db.",
    )
    ap.add_argument(
        "--ids", metavar="ID[,ID,...]", default=None,
        help="Comma-separated character_ids to process.  Required for dry-run "
             "and --gather-only / --commit.  Not used with --commit-from-proposals.",
    )
    ap.add_argument(
        "--book", metavar="SERIES_ORDER", type=int, default=None,
        help="Restrict gather to a single book's chapters (e.g. --book 3 "
             "limits to The Dragon Reborn's text only).  Without this flag, "
             "all chapters in the database are searched.  Useful for per-book "
             "DBs where earlier books' text was already processed in a prior "
             "resolver pass.",
    )
    ap.add_argument(
        "--gather-only", action="store_true",
        help="Run Phase 1 (text search) only.  Writes the gather bundle to "
             "data/origins_gather_<dbname>.json.  No API calls; no DB writes.",
    )
    ap.add_argument(
        "--commit", action="store_true",
        help="Write resolved nationalities to the database.  Without --commit "
             "the resolve phase is a dry-run that prints proposed changes only.",
    )
    ap.add_argument(
        "--commit-from-proposals", metavar="PATH", dest="commit_from_proposals",
        help="Read a saved dry-run proposals JSON and apply the would_write "
             "entries to the database WITHOUT calling the API.  Requires --db.",
    )
    ap.add_argument(
        "--allow-db-mismatch", action="store_true", dest="allow_db_mismatch",
        help="When using --commit-from-proposals, allow the db_file recorded "
             "in the proposals to differ from --db (e.g. if the database was "
             "moved since the dry-run).",
    )
    args = ap.parse_args()

    # ── Mutual-exclusion guards ───────────────────────────────────────────────
    if args.commit and args.gather_only:
        sys.exit("ERROR: --commit and --gather-only cannot be used together.")
    if args.commit and args.commit_from_proposals:
        sys.exit(
            "ERROR: --commit and --commit-from-proposals cannot be used together."
        )
    if args.gather_only and args.commit_from_proposals:
        sys.exit(
            "ERROR: --gather-only and --commit-from-proposals cannot be used together."
        )

    # ── Resolve DB path ───────────────────────────────────────────────────────
    db_path = args.db if args.db is not None else DB_PATH

    # ── --commit-from-proposals path (no gather, no API) ─────────────────────
    if args.commit_from_proposals:
        if args.db is None:
            sys.exit(
                "ERROR: --commit-from-proposals requires --db so we know which "
                "database to write to.  The proposals file's db_file is used only "
                "for validation, not as the implicit target."
            )
        commit_from_proposals(
            args.commit_from_proposals,
            db_path,
            allow_db_mismatch=args.allow_db_mismatch,
        )
        return

    # ── --ids is required for every non-commit-from-proposals mode ───────────
    if args.ids is None:
        sys.exit(
            "ERROR: --ids is required.  Provide comma-separated character_ids, "
            "e.g. --ids 26,138,163."
        )
    try:
        ids = [int(x.strip()) for x in args.ids.split(",") if x.strip()]
    except ValueError:
        sys.exit(
            "ERROR: --ids must be comma-separated integers, e.g. --ids 26,138,163"
        )
    if not ids:
        sys.exit("ERROR: --ids is empty — provide at least one character_id.")

    # ── Phase 1: GATHER ───────────────────────────────────────────────────────
    conn                  = open_db_ro(db_path)
    bundle, chapter_texts = run_gather(conn, ids, db_path, book_series_order=args.book)
    conn.close()

    if args.gather_only:
        print("--gather-only set: stopping after Phase 1.  No API calls made.")
        return

    # ── Phase 2: RESOLVE (dry-run writes proposals; --commit writes DB) ───────
    run_resolve(bundle, chapter_texts, db_path, dry_run=not args.commit)


if __name__ == "__main__":
    main()
