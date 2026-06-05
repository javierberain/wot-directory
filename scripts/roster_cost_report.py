#!/usr/bin/env python3
"""
roster_cost_report.py - READ-ONLY per-book extraction cost report.

Reads the extraction JSONs in data/extractions/ for one book and reports whether
per-chapter extraction input-token cost is climbing as the roster grows, so the
decision to add roster-scoping later is driven by data instead of guesswork. It
writes NOTHING — no database and no extraction JSON is created or modified.

Each extraction JSON (named b{book}_c{chapter}.json) carries a "_meta" block with
book_order, chapter_number, input_tokens, output_tokens,
cache_read_input_tokens, cache_creation_input_tokens, and (for books extracted
after the roster_size change) roster_size. Books extracted before that change
have no roster_size; this report prints "not recorded" for those and still
reports whatever token stats are available.

Usage:
    python scripts/roster_cost_report.py --book 6
    python scripts/roster_cost_report.py --book 7 --extractions-dir data/extractions
"""
import argparse
import glob
import json
import os
import statistics

HERE = os.path.dirname(__file__)
DEFAULT_EXTRACTIONS_DIR = os.path.join(HERE, "..", "data", "extractions")

# If the second half of a book's chapters has a median input_tokens more than
# this fraction above the first half, flag it as "climbing". Tune here.
CLIMB_THRESHOLD = 0.15


def load_book_extractions(extractions_dir, book):
    """Return (records, skipped) for one book.

    records: list of dicts {chapter, input_tokens, cache_read, roster_size,
    path}, ordered by chapter number. Malformed/unreadable files are skipped
    with a printed note rather than crashing.
    """
    records = []
    skipped = 0
    pattern = os.path.join(extractions_dir, f"b{book}_c*.json")
    for path in glob.glob(pattern):
        name = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            meta = data.get("_meta", {})
            # Confirm the book via _meta; if absent, trust the filename glob.
            bo = meta.get("book_order")
            if bo is not None and bo != book:
                continue
            chap = meta.get("chapter_number")
            if chap is None:
                # Derive from the filename b{book}_c{chap}.json.
                chap = int(name.split("_c")[1].split(".")[0])
            records.append({
                "chapter":      chap,
                "input_tokens": meta.get("input_tokens"),
                "cache_read":   meta.get("cache_read_input_tokens"),
                "roster_size":  meta.get("roster_size"),
                "path":         name,
            })
        except (json.JSONDecodeError, OSError, ValueError, KeyError,
                IndexError) as exc:
            print(f"  (skipping malformed file {name}: {exc})")
            skipped += 1
    records.sort(key=lambda r: r["chapter"])
    return records, skipped


def _ints(values):
    """Keep only genuine integers (drops None / missing fields)."""
    return [v for v in values if isinstance(v, int)]


def _roster_label(rec):
    rs = rec["roster_size"]
    return f"{rs:,}" if isinstance(rs, int) else "not recorded"


def main():
    ap = argparse.ArgumentParser(
        description="Read-only per-book roster / token cost report.")
    ap.add_argument("--book", type=int, required=True,
                    help="series order of the book to analyze")
    ap.add_argument("--extractions-dir", default=DEFAULT_EXTRACTIONS_DIR,
                    help="directory of extraction JSONs "
                         "(default: data/extractions/)")
    args = ap.parse_args()

    records, skipped = load_book_extractions(args.extractions_dir, args.book)

    print(f"Roster / token cost report -- book {args.book}")
    print(f"  extractions dir: {os.path.abspath(args.extractions_dir)}")
    if skipped:
        print(f"  skipped {skipped} malformed file(s)")

    if not records:
        print(f"  No extractions found for book {args.book}. Nothing to report.")
        return  # graceful exit 0

    print(f"  chapters found: {len(records)}")

    in_tokens = _ints(r["input_tokens"] for r in records)
    cache_tokens = _ints(r["cache_read"] for r in records)

    if in_tokens:
        print(f"  input_tokens           : median "
              f"{int(statistics.median(in_tokens)):,}  total {sum(in_tokens):,}")
    else:
        print("  input_tokens           : not recorded")
    if cache_tokens:
        print(f"  cache_read_input_tokens: median "
              f"{int(statistics.median(cache_tokens)):,}  "
              f"total {sum(cache_tokens):,}")
    else:
        print("  cache_read_input_tokens: not recorded")

    print(f"  roster_size (start, ch {records[0]['chapter']}): "
          f"{_roster_label(records[0])}")
    print(f"  roster_size (end,   ch {records[-1]['chapter']}): "
          f"{_roster_label(records[-1])}")

    # Climbing-trend flag: first-half vs second-half median input_tokens, in
    # chapter order. Needs at least 2 chapters per half to be meaningful.
    ordered = [r["input_tokens"] for r in records
               if isinstance(r["input_tokens"], int)]
    if len(ordered) >= 4:
        mid = len(ordered) // 2
        first = statistics.median(ordered[:mid])
        second = statistics.median(ordered[mid:])
        pct = (second - first) / first if first else 0.0
        if pct > CLIMB_THRESHOLD:
            print(f"  TREND: per-chapter input tokens are climbing within this "
                  f"book (+{pct * 100:.0f}%)")
        else:
            print(f"  TREND: per-chapter input tokens stable within this book "
                  f"({pct * 100:+.0f}%)")
    else:
        print("  TREND: not enough chapters with input_tokens to assess "
              "(need >= 4)")


if __name__ == "__main__":
    main()
