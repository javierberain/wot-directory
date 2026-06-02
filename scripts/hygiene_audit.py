#!/usr/bin/env python3
"""
hygiene_audit.py - Read-only database hygiene auditor for the WoT character
directory.

IMPORTANT — THIS SCRIPT NEVER MODIFIES THE DATABASE.
The connection is opened with SQLite's mode=ro URI flag so any write attempt
raises immediately at the driver level. There is no "fix" or "apply" mode.
All findings are printed for human review; act on them by editing wot.db
directly.

Usage:
    python scripts/hygiene_audit.py              # run all three checks, print report
    python scripts/hygiene_audit.py --with-llm   # add advisory LLM suggestions for
                                                 # ambiguous rows (requires
                                                 # ANTHROPIC_API_KEY)
    python scripts/hygiene_audit.py --db PATH    # audit a specific snapshot file;
                                                 # omit to default to db/wot.db

Run after ingesting each book.  The wordlists and allow-lists near the top of
this file are the primary knobs to tune between runs.  They are intentionally
kept together and clearly labelled so they are easy to find and edit.
"""

import argparse
import os
import pathlib
import re
import shutil
import sqlite3
import sys
from dotenv import load_dotenv
load_dotenv()

# Shared validation/normalization rules. norm(), the wordlists, and the
# classification predicates all live in directory_rules so this read-only
# auditor and the reconciler's write-time gate apply IDENTICAL logic — no more
# "keep these copies in sync" discipline.
from directory_rules import (
    norm,
    GENERIC_ALIAS_EXACT,
    PRIMARY_NAME_ALLOWLIST,
    PLACEHOLDER_TAIL_WORDS,
    ROLE_NOUN_EXACT,
    is_placeholder_name,
    is_title_or_group_name,
    is_collective_name,
)


# ── Paths (same conventions as reconcile.py) ──────────────────────────────────
DB_PATH  = os.path.join(os.path.dirname(__file__), "..", "db", "wot.db")
BAK_PATH = os.path.join(os.path.dirname(__file__), "..", "db",
                         "wot.db.pre-hygiene.bak")

# Model used for the optional LLM advisory pass (same as extract_chapter.py).
MODEL = "claude-sonnet-4-6"

# ── Tuning knobs ──────────────────────────────────────────────────────────────
# The wordlists and allow-list that used to live here (GENERIC_ALIAS_EXACT,
# PRIMARY_NAME_ALLOWLIST, PLACEHOLDER_TAIL_WORDS, ROLE_NOUN_EXACT) now live in
# scripts/directory_rules.py so the auditor and the reconciler share one
# source of truth. Tune them there.

# Dependency count above which a ⚠ marker is printed for B/C rows.
# Raise this number if you want fewer warnings.
HIGH_DEPENDENT_THRESHOLD = 3

# Maximum characters of chapter full_text sent to the LLM advisory prompt.
LLM_TEXT_LIMIT = 20_000

# ── Formatting helpers ────────────────────────────────────────────────────────
_SEP  = "-" * 60
_SEP2 = "=" * 60


def _header(title):
    print(f"{title:-<60}")


def _dep_line(apps, rels, facs):
    parts = []
    if apps:
        parts.append(f"{apps} appearance{'s' if apps != 1 else ''}")
    if rels:
        parts.append(f"{rels} relationship{'s' if rels != 1 else ''}")
    if facs:
        parts.append(f"{facs} faction link{'s' if facs != 1 else ''}")
    base = "    dependents: " + (", ".join(parts) if parts else "none")
    if apps > HIGH_DEPENDENT_THRESHOLD:
        base += "  [!] many appearances"
    return base


# ── Database helpers ──────────────────────────────────────────────────────────

def open_db():
    """Open wot.db in strict read-only mode via the SQLite URI interface.

    Any write attempt (INSERT/UPDATE/DELETE) raises OperationalError immediately
    at the driver level — this is enforced by SQLite itself, not just by
    convention.
    """
    db_path = pathlib.Path(DB_PATH).resolve()
    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")
    # as_uri() produces a well-formed file:/// URI with forward slashes,
    # which is required by SQLite's URI interface on all platforms.
    db_uri = db_path.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True)
    except sqlite3.OperationalError as exc:
        sys.exit(f"Cannot open database in read-only mode: {exc}\n  {db_path}")
    conn.row_factory = sqlite3.Row
    return conn


def take_backup():
    """Copy wot.db to wot.db.pre-hygiene.bak before touching anything."""
    db_path = pathlib.Path(DB_PATH).resolve()
    bak_path = pathlib.Path(BAK_PATH).resolve()
    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")
    shutil.copy2(db_path, bak_path)
    # Copy WAL/SHM sidecar files if they exist (preserves in-flight state).
    for ext in ("-wal", "-shm"):
        sidecar = pathlib.Path(str(db_path) + ext)
        if sidecar.exists():
            shutil.copy2(sidecar, pathlib.Path(str(bak_path) + ext))
            print(f"  (also backed up WAL sidecar {sidecar.name})")
    print(f"Backup written:  {bak_path}")


def dep_counts(conn, character_id):
    """Return (appearances, relationships, faction_links) for one character."""
    apps = conn.execute(
        "SELECT COUNT(*) FROM appearances WHERE character_id = ?",
        (character_id,),
    ).fetchone()[0]
    rels = conn.execute(
        "SELECT COUNT(*) FROM relationships "
        "WHERE character_a = ? OR character_b = ?",
        (character_id, character_id),
    ).fetchone()[0]
    facs = conn.execute(
        "SELECT COUNT(*) FROM character_factions WHERE character_id = ?",
        (character_id,),
    ).fetchone()[0]
    return apps, rels, facs


# ── Classification predicates ─────────────────────────────────────────────────

# These three predicates now delegate to directory_rules so the auditor's
# Check B2 / B1 / C classify rows identically to the reconciler's write-time
# gate. The shared functions take a raw primary_name (they normalise
# internally) and apply the PRIMARY_NAME_ALLOWLIST themselves.

def _is_b2(name):
    """Check B2 — descriptor placeholder for an unnamed walk-on."""
    return is_placeholder_name(name)


def _is_b1(name):
    """Check B1 — 'the ...' title/group name (allow-list + B2 already excluded)."""
    return is_title_or_group_name(name)


def _is_c(character_type, name):
    """Check C — collective/species label rather than a named individual."""
    return is_collective_name(name, character_type)


# ── The three checks ──────────────────────────────────────────────────────────

def check_a(conn):
    """Return list of alias dicts whose full normalised text is a generic form."""
    rows = conn.execute("""
        SELECT a.alias_id, a.alias_text, a.alias_norm, a.alias_type,
               c.character_id, c.primary_name
          FROM aliases a
          JOIN characters c ON c.character_id = a.character_id
         ORDER BY a.alias_norm, c.primary_name
    """).fetchall()

    flagged = []
    for r in rows:
        if r["alias_norm"] in GENERIC_ALIAS_EXACT:
            flagged.append({
                "alias_id":     r["alias_id"],
                "alias_text":   r["alias_text"],
                "alias_type":   r["alias_type"],
                "character_id": r["character_id"],
                "primary_name": r["primary_name"],
            })
    return flagged


def check_b(conn):
    """Return (b1_rows, b2_rows); each is a list of character dicts with dep counts.

    B1 — title or group names  (e.g. "the Amyrlin Seat", "the Trollocs")
    B2 — descriptor placeholders  (e.g. "the weaselly man", "the stableman")
    """
    rows = conn.execute(
        "SELECT character_id, primary_name, character_type "
        "FROM characters ORDER BY primary_name"
    ).fetchall()

    b1, b2 = [], []
    for r in rows:
        nn = norm(r["primary_name"])
        if nn in PRIMARY_NAME_ALLOWLIST:
            continue
        if _is_b2(nn):
            apps, rels, facs = dep_counts(conn, r["character_id"])
            b2.append({
                "character_id":   r["character_id"],
                "primary_name":   r["primary_name"],
                "character_type": r["character_type"],
                "apps": apps, "rels": rels, "facs": facs,
            })
        elif _is_b1(nn):
            apps, rels, facs = dep_counts(conn, r["character_id"])
            b1.append({
                "character_id":   r["character_id"],
                "primary_name":   r["primary_name"],
                "character_type": r["character_type"],
                "apps": apps, "rels": rels, "facs": facs,
            })
    return b1, b2


def check_c(conn):
    """Return list of character dicts that look like collectives or species labels."""
    rows = conn.execute(
        "SELECT character_id, primary_name, character_type "
        "FROM characters "
        "WHERE character_type IN ('trolloc', 'myrddraal') "
        "ORDER BY primary_name"
    ).fetchall()

    flagged = []
    for r in rows:
        nn = norm(r["primary_name"])
        if nn in PRIMARY_NAME_ALLOWLIST:
            continue
        if _is_c(r["character_type"], nn):
            apps, rels, facs = dep_counts(conn, r["character_id"])
            flagged.append({
                "character_id":   r["character_id"],
                "primary_name":   r["primary_name"],
                "character_type": r["character_type"],
                "apps": apps, "rels": rels, "facs": facs,
            })
    return flagged


# ── LLM advisory helpers ──────────────────────────────────────────────────────

def _get_first_chapter(conn, character_id):
    """Return a Row for the character's first chapter, or None."""
    # Prefer the first_chapter_id stored on the characters row.
    row = conn.execute("""
        SELECT ch.chapter_id, ch.title, ch.full_text
          FROM characters c
          JOIN chapters ch ON ch.chapter_id = c.first_chapter_id
         WHERE c.character_id = ?
    """, (character_id,)).fetchone()
    if row:
        return row
    # Fall back to the earliest appearance.
    row = conn.execute("""
        SELECT ch.chapter_id, ch.title, ch.full_text
          FROM appearances a
          JOIN chapters ch ON ch.chapter_id = a.chapter_id
         WHERE a.character_id = ?
         ORDER BY ch.chapter_id ASC
         LIMIT 1
    """, (character_id,)).fetchone()
    return row


_ADVISORY_PROMPT = """\
You are reviewing a single character-database entry for a Wheel of Time
character directory. A data-hygiene audit has flagged this entry as
potentially problematic.

FLAGGED ENTRY
  character_id : {character_id}
  primary_name : {primary_name}
  character_type: {character_type}

CHAPTER WHERE THIS CHARACTER FIRST APPEARS
  Title: {chapter_title}

CHAPTER TEXT ({text_limit} character limit shown below):
{chapter_text}

YOUR TASK
Based ONLY on the chapter text above, decide whether this database entry
should be kept or removed.

Rules you must follow:
  - Judge ONLY from what the chapter text above shows.
  - Do NOT invent facts or rely on any knowledge of the broader series.
  - KEEP     : the entity has individual presence in the chapter — distinct
               actions, dialogue, or traits that make it more than a
               background mention.
  - REMOVE   : this is a generic background element with no individual
               identity in the chapter text.
  - UNCERTAIN: the chapter text does not give enough information to decide.

Respond with exactly this format on a single line:
VERDICT: [KEEP|REMOVE|UNCERTAIN] — [one sentence reason citing the text]
"""


def llm_advisory(entry, chapter_title, chapter_text):
    """Call the Claude API for a keep-or-remove advisory. Returns a string.

    The result is purely advisory and does not affect what the script does.
    """
    try:
        import anthropic
    except ImportError:
        return "ERROR: anthropic package not installed (pip install anthropic)."

    excerpt = (chapter_text or "")[:LLM_TEXT_LIMIT]
    prompt = _ADVISORY_PROMPT.format(
        character_id=entry["character_id"],
        primary_name=entry["primary_name"],
        character_type=entry["character_type"],
        chapter_title=chapter_title,
        text_limit=LLM_TEXT_LIMIT,
        chapter_text=excerpt,
    )
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as exc:
        return f"ERROR calling API: {exc}"


# ── Report printing ───────────────────────────────────────────────────────────

def _print_char_row(r):
    print(f"  character_id={r['character_id']}  "
          f"\"{r['primary_name']}\"  [{r['character_type']}]")
    print(_dep_line(r["apps"], r["rels"], r["facs"]))


# ── Detail-mode and chapter-context helpers ───────────────────────────────────

def _resolve_chapter(conn, chapter_id):
    """Return 'Bk1/Ch14 "Title"' for a chapter_id, or a descriptive fallback."""
    if chapter_id is None:
        return "(null)"
    row = conn.execute("""
        SELECT ch.chapter_number, ch.title, b.series_order
          FROM chapters ch
          JOIN books b ON b.book_id = ch.book_id
         WHERE ch.chapter_id = ?
    """, (chapter_id,)).fetchone()
    if not row:
        return f"(chapter_id={chapter_id} not found in db)"
    return f"Bk{row['series_order']}/Ch{row['chapter_number']} \"{row['title']}\""


def _char_appearances_list(conn, character_id):
    """Return compact chapter labels for every chapter this character appears in.

    Format matches the extraction-file naming convention: b1_c14, b2_c3, etc.
    Ordered by book then chapter number.
    """
    rows = conn.execute("""
        SELECT b.series_order, ch.chapter_number
          FROM appearances ap
          JOIN chapters ch ON ch.chapter_id = ap.chapter_id
          JOIN books b ON b.book_id = ch.book_id
         WHERE ap.character_id = ?
         ORDER BY b.series_order, ch.chapter_number
    """, (character_id,)).fetchall()
    return [f"b{r['series_order']}_c{r['chapter_number']}" for r in rows]


def _print_char_row_with_chapters(conn, r):
    """Print the standard audit row, then add first_chapter_id and appearance list.

    Calls _print_char_row() first so its output is preserved exactly, then
    appends two lines: the resolved first_chapter_id pointer from the characters
    table, and the full list of chapters where this character actually has an
    appearances row (both from appearances joined to chapters/books).

    The two-source comparison is intentional: a mismatch between first_chapter_id
    and the earliest appearance chapter is itself a useful signal.
    """
    _print_char_row(r)
    # first_chapter_id is not in the check dicts; fetch it with a targeted query.
    fch_row = conn.execute(
        "SELECT first_chapter_id FROM characters WHERE character_id = ?",
        (r["character_id"],),
    ).fetchone()
    first_ch_id = fch_row["first_chapter_id"] if fch_row else None
    print(f"    first_chapter_id: {_resolve_chapter(conn, first_ch_id)}")
    chaps = _char_appearances_list(conn, r["character_id"])
    if chaps:
        print(f"    appears in: {', '.join(chaps)}")
    else:
        print("    appears in: (no appearance rows)")


# Column order mirrors schema.sql declaration order for the characters table.
_CHAR_COLS = (
    "character_id", "primary_name", "character_type", "nationality",
    "physical_traits", "age", "filiations", "associations", "personality",
    "description", "first_chapter_id", "created_at", "updated_at",
)


def print_detail(conn, character_id):
    """Print a complete read-only dossier for a single character.

    Covers every row in every table that references this character_id:
    the characters row itself, all aliases, all appearances (with resolved
    chapter context), all relationships (with the other party named), and
    all faction memberships.  Nothing is truncated.  This is the pre-deletion
    checklist: run it to see exactly what dependent data would be orphaned.
    """
    char = conn.execute(
        "SELECT * FROM characters WHERE character_id = ?",
        (character_id,),
    ).fetchone()
    if not char:
        sys.exit(
            f"No character with character_id={character_id} found in the database."
        )

    print()
    print(_SEP2)
    print(f"  CHARACTER DOSSIER  --  character_id={character_id}")
    print(_SEP2)

    # ── characters row ────────────────────────────────────────────────────────
    print()
    _header("CHARACTERS ROW ")
    for col in _CHAR_COLS:
        if col == "first_chapter_id":
            val = char[col]
            resolved = _resolve_chapter(conn, val)
            print(f"  {'first_chapter_id':<18}: {val}  {resolved}")
        else:
            val = char[col]
            print(f"  {col:<18}: {val if val is not None else '(null)'}")

    # ── aliases ───────────────────────────────────────────────────────────────
    aliases = conn.execute("""
        SELECT alias_id, alias_text, alias_norm, alias_type, is_primary, notes
          FROM aliases
         WHERE character_id = ?
         ORDER BY is_primary DESC, alias_type, alias_text
    """, (character_id,)).fetchall()
    print()
    _header(f"ALIASES ({len(aliases)}) ")
    if aliases:
        for a in aliases:
            primary_flag = "  [PRIMARY]" if a["is_primary"] else ""
            print(f"  alias_id={a['alias_id']}  "
                  f"\"{a['alias_text']}\"{primary_flag}")
            print(f"    alias_norm={a['alias_norm']}"
                  f"  type={a['alias_type']}"
                  f"  is_primary={a['is_primary']}")
            if a["notes"]:
                print(f"    notes: {a['notes']}")
    else:
        print("  (none)")

    # ── appearances ───────────────────────────────────────────────────────────
    apps = conn.execute("""
        SELECT ap.appearance_id, ap.name_used, ap.whereabouts,
               ap.notable_actions, ap.alliances_shown, ap.demeanor,
               ch.chapter_id, ch.chapter_number, ch.title AS ch_title,
               b.series_order, b.title AS book_title
          FROM appearances ap
          JOIN chapters ch ON ch.chapter_id = ap.chapter_id
          JOIN books b ON b.book_id = ch.book_id
         WHERE ap.character_id = ?
         ORDER BY b.series_order, ch.chapter_number
    """, (character_id,)).fetchall()
    print()
    _header(f"APPEARANCES ({len(apps)}) ")
    if apps:
        for ap in apps:
            print(f"  [Bk{ap['series_order']} Ch{ap['chapter_number']}:"
                  f" \"{ap['ch_title']}\"]")
            print(f"  name_used       : {ap['name_used'] or '(null)'}")
            print(f"  whereabouts     : {ap['whereabouts'] or '(null)'}")
            print(f"  notable_actions : {ap['notable_actions'] or '(null)'}")
            print(f"  alliances_shown : {ap['alliances_shown'] or '(null)'}")
            print(f"  demeanor        : {ap['demeanor'] or '(null)'}")
            print()
    else:
        print("  (none)")
        print()

    # ── relationships ─────────────────────────────────────────────────────────
    # The CASE expression resolves the "other" end of each edge regardless of
    # which column this character's id sits in.
    rels = conn.execute("""
        SELECT r.relationship_id, r.relationship_type, r.directed,
               r.description, r.first_chapter_id,
               CASE WHEN r.character_a = ? THEN r.character_b
                    ELSE r.character_a END AS other_id,
               c.primary_name AS other_name,
               ch.chapter_number AS first_ch_num,
               ch.title         AS first_ch_title,
               b.series_order   AS first_bk
          FROM relationships r
          JOIN characters c
            ON c.character_id = (CASE WHEN r.character_a = ?
                                       THEN r.character_b
                                       ELSE r.character_a END)
          LEFT JOIN chapters ch ON ch.chapter_id = r.first_chapter_id
          LEFT JOIN books    b  ON b.book_id = ch.book_id
         WHERE r.character_a = ? OR r.character_b = ?
         ORDER BY r.relationship_type, c.primary_name
    """, (character_id, character_id, character_id, character_id)).fetchall()
    print()
    _header(f"RELATIONSHIPS ({len(rels)}) ")
    if rels:
        for r in rels:
            directed_str = "directed" if r["directed"] else "undirected"
            print(f"  [{r['relationship_type']} / {directed_str}]")
            print(f"  other party     : character_id={r['other_id']}"
                  f"  \"{r['other_name']}\"")
            desc = r["description"] or "(null)"
            print(f"  description     : {desc}")
            if r["first_ch_num"] is not None:
                print(f"  first chapter   : Bk{r['first_bk']}/Ch{r['first_ch_num']}"
                      f" \"{r['first_ch_title']}\"")
            else:
                print("  first chapter   : (null)")
            print()
    else:
        print("  (none)")
        print()

    # ── factions ──────────────────────────────────────────────────────────────
    factions = conn.execute("""
        SELECT f.name AS faction_name, f.faction_type,
               cf.role, cf.notes, cf.first_chapter_id,
               ch.chapter_number AS first_ch_num,
               ch.title          AS first_ch_title,
               b.series_order    AS first_bk
          FROM character_factions cf
          JOIN factions f ON f.faction_id = cf.faction_id
          LEFT JOIN chapters ch ON ch.chapter_id = cf.first_chapter_id
          LEFT JOIN books    b  ON b.book_id = ch.book_id
         WHERE cf.character_id = ?
         ORDER BY f.name
    """, (character_id,)).fetchall()
    print()
    _header(f"FACTIONS ({len(factions)}) ")
    if factions:
        for f in factions:
            print(f"  \"{f['faction_name']}\"  [{f['faction_type']}]")
            print(f"  role: {f['role']}")
            if f["first_ch_num"] is not None:
                print(f"  first chapter: Bk{f['first_bk']}/Ch{f['first_ch_num']}"
                      f" \"{f['first_ch_title']}\"")
            else:
                print("  first chapter: (null)")
            if f["notes"]:
                print(f"  notes: {f['notes']}")
            print()
    else:
        print("  (none)")
        print()


def main():
    ap = argparse.ArgumentParser(
        description="Read-only hygiene audit for the WoT character database.",
    )
    ap.add_argument(
        "--with-llm", action="store_true",
        help="Add Claude API advisory suggestions for ambiguous flagged rows "
             "(requires ANTHROPIC_API_KEY).",
    )
    ap.add_argument(
        "--detail", type=int, metavar="CHARACTER_ID",
        help="Print a complete read-only dossier for one character and exit. "
             "Skips the full audit and the backup step.",
    )
    ap.add_argument(
        "--db", metavar="PATH",
        help="Path to the SQLite database file to audit. "
             "Defaults to db/wot.db (the live ingestion database).",
    )
    args = ap.parse_args()

    # ── Resolve DB/BAK paths from --db if given ───────────────────────────────
    if args.db is not None:
        global DB_PATH, BAK_PATH
        DB_PATH = args.db
        BAK_PATH = args.db + ".pre-hygiene.bak"

    # ── Detail mode: read-only dossier for one character, then exit ───────────
    # Intentionally placed before the --with-llm check and the backup step.
    # --detail is a pure viewer; it does not imply any upcoming cleanup action.
    if args.detail is not None:
        conn = open_db()
        print_detail(conn, args.detail)
        conn.close()
        return

    if args.with_llm and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "--with-llm requires the ANTHROPIC_API_KEY environment variable.\n"
            "Set it with:  export ANTHROPIC_API_KEY=sk-..."
        )

    # ── Step 0: backup ────────────────────────────────────────────────────────
    take_backup()

    # ── Open DB in strict read-only mode ──────────────────────────────────────
    conn = open_db()

    char_count  = conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
    alias_count = conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]

    print()
    print(_SEP2)
    print("  WoT CHARACTER DIRECTORY -- HYGIENE AUDIT")
    print(f"  {char_count} characters  |  {alias_count} aliases")
    print(_SEP2)
    print()

    # ── Check A ───────────────────────────────────────────────────────────────
    a_rows = check_a(conn)

    _header("CHECK A: GENERIC ALIASES ")
    print("Aliases whose entire normalised text is a generic form of address.")
    print("These pollute the matching index and should be removed from aliases.")
    print()
    if a_rows:
        for r in a_rows:
            print(f"  alias_id={r['alias_id']}  "
                  f"\"{r['alias_text']}\"  [{r['alias_type']}]")
            print(f"    -> character_id={r['character_id']}"
                  f"  \"{r['primary_name']}\"")
        print()
    else:
        print("  (none found)")
        print()

    # ── Check B1 ──────────────────────────────────────────────────────────────
    b1_rows, b2_rows = check_b(conn)

    _header("CHECK B1: TITLE / GROUP PRIMARY NAMES ")
    print("Primary names that look like formal titles or group/organisation names")
    print("rather than individual character names.  Allow-listed names are spared.")
    print()
    if b1_rows:
        for r in b1_rows:
            _print_char_row_with_chapters(conn, r)
        print()
    else:
        print("  (none found)")
        print()

    # ── Likely renames sub-section ────────────────────────────────────────────
    rename_candidates = [r for r in b1_rows if r["apps"] > 2]
    _header("LIKELY RENAMES, NOT REMOVALS ")
    print("Heuristic: a B1 character with more than 2 appearances is almost")
    print("certainly a real character with a wrong primary_name, not a row to")
    print("delete.  Resolve by finding the proper name and updating primary_name.")
    print()
    if rename_candidates:
        for r in rename_candidates:
            print(f"  character_id={r['character_id']}  \"{r['primary_name']}\""
                  f"  [{r['character_type']}]  --  {r['apps']} appearances")
        print()
    else:
        print("  (none -- all B1 rows have 2 or fewer appearances)")
        print()

    # ── Check B2 ──────────────────────────────────────────────────────────────
    _header("CHECK B2: DESCRIPTOR PLACEHOLDERS ")
    print("Primary names that look like extractor-invented placeholders for")
    print("unnamed walk-on characters.  Each may or may not deserve a row.")
    print()
    if b2_rows:
        for r in b2_rows:
            _print_char_row_with_chapters(conn, r)
        print()
    else:
        print("  (none found)")
        print()

    # ── Check C ───────────────────────────────────────────────────────────────
    c_rows = check_c(conn)

    _header("CHECK C: NON-INDIVIDUAL CHARACTER ROWS ")
    print("Rows whose character_type and name together suggest a collective,")
    print("species label, or generic creature rather than a named individual.")
    print("(Named individuals like Narg are not flagged.)")
    print()
    if c_rows:
        for r in c_rows:
            _print_char_row_with_chapters(conn, r)
        print()
    else:
        print("  (none found)")
        print()

    # ── LLM advisory ──────────────────────────────────────────────────────────
    if args.with_llm:
        # Ambiguous = B2 rows (walk-on placeholders: could be meaningful)
        #           + B1 rows that have at least one appearance (worth checking)
        # NOT ambiguous: C rows (clearly collective/species labels)
        # NOT ambiguous: B1 rows with zero appearances (dead data, obvious)
        ambiguous = list(b2_rows) + [r for r in b1_rows if r["apps"] > 0]

        print()
        _header("ADVISORY (--with-llm) ")
        print("Claude's keep-or-remove suggestion for each ambiguous flagged row.")
        print("THIS IS ADVISORY ONLY.  The script has not modified the database.")
        print()

        if not ambiguous:
            print("  (no ambiguous rows identified for LLM review)")
            print()
        else:
            for r in ambiguous:
                ch = _get_first_chapter(conn, r["character_id"])
                print(f"  character_id={r['character_id']}"
                      f"  \"{r['primary_name']}\"")
                if not ch:
                    print("    first chapter: (none found)")
                    print("    ADVISORY: UNCERTAIN — no chapter text available.")
                    print()
                    continue
                print(f"    first chapter: \"{ch['title']}\"")
                verdict = llm_advisory(r, ch["title"], ch["full_text"])
                # Indent multi-line verdicts consistently.
                indented = verdict.replace("\n", "\n    ")
                print(f"    ADVISORY: {indented}")
                print()

    # ── Summary ───────────────────────────────────────────────────────────────
    unique_chars = len({
        r["character_id"]
        for lst in (b1_rows, b2_rows, c_rows)
        for r in lst
    })

    print(_SEP2)
    print("  SUMMARY")
    print(_SEP2)
    print(f"  Check A  - generic aliases:          {len(a_rows):4d} flagged")
    print(f"  Check B1 - title/group names:        {len(b1_rows):4d} flagged")
    print(f"  Check B2 - placeholder names:        {len(b2_rows):4d} flagged")
    print(f"  Check C  - non-individual rows:      {len(c_rows):4d} flagged")
    print(f"  Unique characters to review (B+C):   {unique_chars:4d}")
    print()
    print("  Action: review the above and edit wot.db directly.")
    print("  This script has not modified the database.")
    print()

    conn.close()


if __name__ == "__main__":
    main()
