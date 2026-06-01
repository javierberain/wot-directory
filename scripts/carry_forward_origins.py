#!/usr/bin/env python3
"""
carry_forward_origins.py - Propagate resolved nationality values from
data/origins_resolved.csv into a target database.

The CSV is written by resolve_origins.py after a --commit run.  Each row
carries a character_id that is meaningful ONLY in the db_file it was resolved
in — character_ids are per-db and must never be used to identify characters
across snapshots.  This script matches by normalised primary_name instead.

Forward-only propagation
    A value resolved at book N may propagate into book M only if M >= N.
    Use --max-source-book N to exclude CSV rows from later books.
    The book number is parsed from the db_file path (looks for wot_bookN or
    wot_bookN_cleanup).  If a db_file doesn't match either pattern, the row
    is skipped with a notice — never guessed.

Matching discipline
    - 0 matches  → SKIP  (character absent from this snapshot)
    - 1 match    → proceed to write-eligibility check
    - 2+ matches → SKIP  (AMBIGUOUS — never guess)

Write-eligibility (matched characters only)
    - NULL / empty / "unknown..." nationality → eligible, mark WOULD WRITE
    - already matches CSV value (normalised)  → ALREADY MATCHES, skip (idempotent)
    - real value, different from CSV          → CONFLICT, skip — never overwrite

Usage:
    python scripts/carry_forward_origins.py \\
        --csv data/origins_resolved.csv \\
        --target db/wot_book2.db \\
        --max-source-book 2

    python scripts/carry_forward_origins.py \\
        --csv data/origins_resolved.csv \\
        --target db/wot_book2.db \\
        --max-source-book 2 --commit

    --csv and --target are both required (no defaults — propagating resolved
    values is always a deliberate cross-db action).
    --commit writes to --target; without it the script is a dry-run.
    --max-source-book is strongly recommended to prevent later-book values
    from leaking backward across snapshots.
"""

import argparse
import csv
import pathlib
import re
import shutil
import sqlite3
import sys


# ── Formatting helpers ────────────────────────────────────────────────────────
_SEP  = "-" * 70
_SEP2 = "=" * 70

_CSV_REQUIRED_COLS = frozenset(
    {"db_file", "character_id", "primary_name", "nationality", "basis", "evidence"}
)


def _header(title):
    print(f"\n{title:-<70}")


# ── Text normalisation (mirrors resolve_origins.py exactly) ───────────────────

def norm_for_search(text):
    """Lowercase + smart-apostrophe → straight + collapse whitespace.

    Identical to norm_for_search() in resolve_origins.py.  Duplicated here so
    this script has no import dependency on that module and the behaviour stays
    coupled at the source level — update both if the normalisation changes.
    """
    text = text.lower()
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_placeholder(nationality):
    """Return True if nationality is NULL, empty, or starts with 'unknown'.

    Identical to is_placeholder() in resolve_origins.py.
    """
    if nationality is None or str(nationality).strip() == "":
        return True
    return str(nationality).strip().lower().startswith("unknown")


# ── Book-number extraction ────────────────────────────────────────────────────

# Matches "wot_book1", "wot_book2_cleanup", "wot_book3.db.pre-origins.bak",
# etc.  Case-insensitive so Windows path capitalisation doesn't matter.
_BOOK_RE = re.compile(r"wot_book(\d+)", re.IGNORECASE)


def parse_book_number(db_file):
    """Return the integer book number embedded in db_file, or None.

    Matches wot_bookN and wot_bookN_cleanup (and their .bak variants).
    Returns None for paths that don't match (e.g. wot.db, /some/other.db).
    """
    m = _BOOK_RE.search(db_file)
    return int(m.group(1)) if m else None


# ── Action constants ──────────────────────────────────────────────────────────

WOULD_WRITE    = "would_write"
ALREADY_MATCH  = "already_matches"
CONFLICT       = "conflict"
NOT_FOUND      = "not_found"
AMBIGUOUS      = "ambiguous"
FILTERED       = "filtered_by_book"
NO_BOOK_NUM    = "skipped_no_book_number"

# Display labels for each action, used in both per-row output and the summary.
_ACTION_LABEL = {
    WOULD_WRITE:   "WOULD WRITE",
    ALREADY_MATCH: "ALREADY MATCHES",
    CONFLICT:      "CONFLICT — kept existing",
    NOT_FOUND:     "NOT FOUND in target",
    AMBIGUOUS:     "AMBIGUOUS in target",
    FILTERED:      "FILTERED (source book > max)",
    NO_BOOK_NUM:   "SKIPPED (no book number in db_file)",
}


# ── Database helpers ──────────────────────────────────────────────────────────

def open_target(path):
    """Open the target database read-write with foreign-key enforcement."""
    p = pathlib.Path(path).resolve()
    if not p.exists():
        sys.exit(f"ERROR: target database not found: {p}")
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def take_backup(target_path):
    """Copy --target to <target>.pre-carry-forward.bak before any writes.

    Copies WAL/SHM sidecar files if they exist — same pattern as other scripts.
    """
    p   = pathlib.Path(target_path).resolve()
    bak = pathlib.Path(str(p) + ".pre-carry-forward.bak")
    shutil.copy2(p, bak)
    for ext in ("-wal", "-shm"):
        sidecar = pathlib.Path(str(p) + ext)
        if sidecar.exists():
            shutil.copy2(sidecar, pathlib.Path(str(bak) + ext))
            print(f"  (backed up WAL sidecar {sidecar.name})")
    print(f"  Backup written: {bak}")


# ── Name index ────────────────────────────────────────────────────────────────

def build_name_index(conn):
    """Return {norm_name: [row_dict, ...]} for every character in --target.

    Fetched once and reused for all CSV rows.  The value is a list because two
    characters could theoretically normalise to the same string; the caller
    handles that case as AMBIGUOUS.

    Each row_dict carries character_id, primary_name, and nationality.
    """
    rows = conn.execute(
        "SELECT character_id, primary_name, nationality FROM characters"
    ).fetchall()
    index = {}
    for r in rows:
        key = norm_for_search(r["primary_name"])
        index.setdefault(key, [])
        index[key].append({
            "character_id": r["character_id"],
            "primary_name": r["primary_name"],
            "nationality":  r["nationality"],
        })
    return index


# ── Per-row decision ──────────────────────────────────────────────────────────

def decide_row(csv_row, name_index):
    """Evaluate one already-filtered CSV row against the target name index.

    Returns a result dict with keys:
        action       : one of the ACTION constants
        primary_name : str (from CSV)
        csv_nat      : str (nationality from CSV)
        basis        : str (from CSV)
        db_file      : str (from CSV)
        book_number  : int (already verified non-None and within range)
        character_id : int or None  (target match, if exactly one found)
        current_nat  : str or None  (current target value, if matched)
        candidates   : list of dicts  (populated only for AMBIGUOUS)
    """
    pname     = csv_row["primary_name"]
    csv_nat   = csv_row["nationality"].strip()
    basis     = csv_row["basis"].strip()
    db_file   = csv_row["db_file"]
    book_num  = parse_book_number(db_file)  # already confirmed not None

    result = {
        "primary_name": pname,
        "csv_nat":      csv_nat,
        "basis":        basis,
        "db_file":      db_file,
        "book_number":  book_num,
        "character_id": None,
        "current_nat":  None,
        "candidates":   [],
        "action":       None,
    }

    # ── Name-based lookup ─────────────────────────────────────────────────────
    norm_name  = norm_for_search(pname)
    candidates = name_index.get(norm_name, [])

    if len(candidates) == 0:
        result["action"] = NOT_FOUND
        return result

    if len(candidates) > 1:
        result["action"]     = AMBIGUOUS
        result["candidates"] = candidates
        return result

    # Exactly one match.
    target_char = candidates[0]
    result["character_id"] = target_char["character_id"]
    result["current_nat"]  = target_char["nationality"]

    # ── Write-eligibility ─────────────────────────────────────────────────────
    if not is_placeholder(target_char["nationality"]):
        # Character already has a real nationality.  Check for idempotency.
        if (norm_for_search(target_char["nationality"] or "")
                == norm_for_search(csv_nat)):
            result["action"] = ALREADY_MATCH
        else:
            result["action"] = CONFLICT
        return result

    result["action"] = WOULD_WRITE
    return result


# ── Output helpers ────────────────────────────────────────────────────────────

def print_row_dossier(res):
    """Print the per-row dossier for one eligible CSV row."""
    label = _ACTION_LABEL[res["action"]]
    print(f"\n  {res['primary_name']!r}  (source: Bk{res['book_number']})")
    print(f"    csv nationality : {res['csv_nat']!r}  [{res['basis']}]")

    if res["character_id"] is not None:
        print(f"    target char_id  : {res['character_id']}")
        print(f"    target nat now  : {res['current_nat']!r}")
    else:
        print(f"    target char_id  : (not matched)")

    print(f"    decision        : {label}")

    if res["action"] == AMBIGUOUS:
        print(f"    candidates:")
        for c in res["candidates"]:
            print(f"      character_id={c['character_id']}  "
                  f"\"{c['primary_name']}\"  nationality={c['nationality']!r}")

    if res["action"] == CONFLICT:
        print(f"    kept    : {res['current_nat']!r}  (target)")
        print(f"    ignored : {res['csv_nat']!r}  (csv)")


def print_summary(n_total, n_no_book, n_filtered, eligible_results,
                  max_source_book):
    """Print the aggregate summary counts."""
    counts = {k: 0 for k in _ACTION_LABEL}
    for r in eligible_results:
        counts[r["action"]] += 1

    _header("SUMMARY")
    print()
    if max_source_book is not None:
        print(f"  --max-source-book : {max_source_book}")
    print(f"  {'Total CSV rows':<32}: {n_total}")
    print(f"  {'Skipped (no book number in path)':<32}: {n_no_book}")
    print(f"  {'Filtered (source book > max)':<32}: {n_filtered}")
    print(f"  {'Eligible (evaluated below)':<32}: "
          f"{n_total - n_no_book - n_filtered}")
    print()
    print(f"  {'  would_write':<32}: {counts[WOULD_WRITE]}")
    print(f"  {'  already_matches':<32}: {counts[ALREADY_MATCH]}")
    print(f"  {'  conflict':<32}: {counts[CONFLICT]}")
    print(f"  {'  not_found':<32}: {counts[NOT_FOUND]}")
    print(f"  {'  ambiguous':<32}: {counts[AMBIGUOUS]}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=(
            "Propagate resolved nationality values from origins_resolved.csv "
            "into a target database.  Matches by normalised primary_name, "
            "never by character_id.  Never overwrites a real nationality."
        ),
    )
    ap.add_argument(
        "--csv", required=True, metavar="PATH",
        help="Path to origins_resolved.csv (written by resolve_origins.py).",
    )
    ap.add_argument(
        "--target", required=True, metavar="PATH",
        help="Database to apply resolved values into (read-write).",
    )
    ap.add_argument(
        "--max-source-book", type=int, metavar="N", dest="max_source_book",
        help=(
            "Only apply CSV rows whose db_file refers to book <= N.  "
            "E.g. --max-source-book 2 when targeting wot_book2.db.  "
            "Omit to process all rows (not recommended across book boundaries)."
        ),
    )
    ap.add_argument(
        "--commit", action="store_true",
        help=(
            "Write resolved nationalities to --target.  Without --commit the "
            "script is a dry-run: it prints the per-row dossier and summary "
            "but makes no changes."
        ),
    )
    args = ap.parse_args()

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print()
    print(_SEP2)
    print("  WoT CHARACTER DIRECTORY — CARRY FORWARD ORIGINS")
    print(f"  Mode             : {mode}")
    print(f"  csv              : {pathlib.Path(args.csv).resolve()}")
    print(f"  target           : {pathlib.Path(args.target).resolve()}")
    if args.max_source_book is not None:
        print(f"  --max-source-book: {args.max_source_book}")
    else:
        print(f"  --max-source-book: (none — all rows eligible)")
    print(_SEP2)

    # ── Step 1: read the CSV ──────────────────────────────────────────────────
    csv_path = pathlib.Path(args.csv).resolve()
    if not csv_path.exists():
        sys.exit(f"\nERROR: CSV file not found: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader   = csv.DictReader(f)
        csv_rows = list(reader)

    if not csv_rows:
        sys.exit(
            "\nERROR: CSV is empty or header-only.\n"
            "Run resolve_origins.py --commit to populate it first."
        )

    missing_cols = _CSV_REQUIRED_COLS - set(csv_rows[0].keys())
    if missing_cols:
        sys.exit(
            f"\nERROR: CSV is missing expected columns: "
            f"{sorted(missing_cols)}\n"
            f"  Found: {sorted(csv_rows[0].keys())}"
        )

    n_total = len(csv_rows)
    print(f"\n  {n_total} row(s) read from CSV.")

    # ── Step 2: forward-only filter ───────────────────────────────────────────
    # Split into three buckets before touching the target db.
    no_book_rows  = []   # db_file has no recognisable wot_bookN pattern
    filtered_rows = []   # recognised but book > max_source_book
    eligible_rows = []   # will be evaluated against target

    for row in csv_rows:
        book_num = parse_book_number(row["db_file"])
        if book_num is None:
            no_book_rows.append(row)
        elif (args.max_source_book is not None
              and book_num > args.max_source_book):
            filtered_rows.append(row)
        else:
            eligible_rows.append(row)

    n_no_book  = len(no_book_rows)
    n_filtered = len(filtered_rows)
    n_eligible = len(eligible_rows)

    if n_no_book:
        print(f"  {n_no_book} row(s) skipped: no recognisable book number "
              f"in db_file path.")
        for row in no_book_rows:
            print(f"    db_file={row['db_file']!r}  "
                  f"name={row['primary_name']!r}")

    if n_filtered:
        print(f"  {n_filtered} row(s) filtered: source book > "
              f"{args.max_source_book}.")

    print(f"  {n_eligible} row(s) eligible for evaluation.")

    if n_eligible == 0:
        print()
        print("  Nothing to evaluate — all rows were filtered or skipped.")
        return

    # ── Step 3: open target, build name index ─────────────────────────────────
    tgt        = open_target(args.target)
    name_index = build_name_index(tgt)
    print(f"  {len(name_index)} normalised name(s) loaded from target.")

    # ── Step 4: evaluate each eligible row ────────────────────────────────────
    _header("PER-ROW DECISIONS")
    results = []
    for row in eligible_rows:
        res = decide_row(row, name_index)
        results.append(res)
        print_row_dossier(res)

    # ── Step 5: summary ───────────────────────────────────────────────────────
    print_summary(n_total, n_no_book, n_filtered, results, args.max_source_book)

    # ── Step 6: dry-run exit ──────────────────────────────────────────────────
    writable = [r for r in results if r["action"] == WOULD_WRITE]

    if not args.commit:
        print()
        print(_SEP)
        print("  Dry-run complete.  No changes made.")
        if writable:
            print(f"  {len(writable)} value(s) would be written with --commit.")
        else:
            print("  Nothing to write.")
        tgt.close()
        return

    if not writable:
        print()
        print("  Nothing to write.  Target is already up to date.")
        tgt.close()
        return

    # ── Step 6 (commit): backup + write ───────────────────────────────────────
    _header("WRITING")
    take_backup(args.target)
    print()

    try:
        for r in writable:
            tgt.execute(
                "UPDATE characters SET nationality = ? "
                "WHERE character_id = ?",
                (r["csv_nat"], r["character_id"]),
            )
        tgt.commit()
    except Exception as exc:
        tgt.rollback()
        tgt.close()
        sys.exit(
            f"\nERROR during write: {exc}\n"
            f"All changes rolled back.  Target database is unchanged."
        )

    # ── Verify each written row ───────────────────────────────────────────────
    _header("VERIFICATION")
    print()
    verify_errors = []

    for r in writable:
        row = tgt.execute(
            "SELECT nationality FROM characters WHERE character_id = ?",
            (r["character_id"],),
        ).fetchone()
        if row is None:
            verify_errors.append(
                f"character_id={r['character_id']} "
                f"\"{r['primary_name']}\" not found after commit!"
            )
            continue
        got = row["nationality"]
        if norm_for_search(got or "") == norm_for_search(r["csv_nat"]):
            print(f"  WRITTEN  character_id={r['character_id']:<6}  "
                  f"\"{r['primary_name']}\"  "
                  f"{r['current_nat']!r}  →  {got!r}")
        else:
            verify_errors.append(
                f"character_id={r['character_id']} "
                f"\"{r['primary_name']}\": "
                f"expected {r['csv_nat']!r}, got {got!r}"
            )

    if verify_errors:
        print()
        print("  WARNING: verification found discrepancies:")
        for e in verify_errors:
            print(f"    {e}")
        print("  Investigate before relying on the updated data.")
    else:
        print()
        print(f"  All {len(writable)} update(s) verified.")

    tgt.close()


if __name__ == "__main__":
    main()
