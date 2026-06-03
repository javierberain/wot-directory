#!/usr/bin/env python3
"""
run_book.py - Extract and reconcile every un-processed chapter of a book,
in order. This is the "feed it chapter by chapter" loop, automated.

It walks chapters in series order so the roster grows naturally: each
chapter's extraction sees every character found in the chapters before it.

Usage:
    export ANTHROPIC_API_KEY=sk-...
    python run_book.py --book 1                 # all chapters
    python run_book.py --book 1 --from 0 --to 10
    python run_book.py --book 1 --auto          # auto-commit confident items

By default it pauses after each chapter so you can inspect the extraction
JSON before it commits. Pass --auto to run straight through.
"""
import argparse
import os
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(__file__)
DB_DIR = os.path.join(HERE, "..", "db")


def working_db(book_order):
    """The per-book snapshot is the working DB (snapshot-as-working-DB model).
    Book N is parsed, reconciled, and cleaned in db/wot_book{N}.db — never the
    legacy scratch db/wot.db."""
    return os.path.join(DB_DIR, f"wot_book{book_order}.db")


def chapters_for(book_order, lo, hi, db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT ch.chapter_number, ch.title, ch.extracted
        FROM chapters ch JOIN books b ON b.book_id = ch.book_id
        WHERE b.series_order = ?
        ORDER BY ch.chapter_number
    """, (book_order,)).fetchall()
    conn.close()
    out = []
    for num, title, extracted in rows:
        if lo is not None and num < lo:
            continue
        if hi is not None and num > hi:
            continue
        out.append((num, title, extracted))
    return out


def run(cmd):
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", type=int, required=True)
    ap.add_argument("--from", dest="lo", type=int, default=None)
    ap.add_argument("--to", dest="hi", type=int, default=None)
    ap.add_argument("--auto", action="store_true",
                    help="commit confident items, no pause")
    ap.add_argument("--skip-extracted", action="store_true", default=True,
                    help="skip chapters already marked extracted")
    ap.add_argument("--db", help="override working DB (default: "
                    "db/wot_book{N}.db)")
    args = ap.parse_args()

    db = args.db or working_db(args.book)
    if not os.path.exists(db):
        sys.exit(f"Working DB not found: {db}\n"
                 f"Seed it first:  python scripts/start_book.py --book "
                 f"{args.book} --epub \"<file>\" --title \"<title>\"")

    chapters = chapters_for(args.book, args.lo, args.hi, db)
    if not chapters:
        sys.exit("No chapters match. Did you run start_book.py?")

    print(f"Book {args.book}: {len(chapters)} chapter(s) in range. "
          f"Working DB: {db}\n")

    for num, title, extracted in chapters:
        if extracted and args.skip_extracted:
            print(f"-- Ch {num} '{title}' already extracted, skipping.")
            continue

        print(f"\n{'='*60}\nChapter {num}: {title}\n{'='*60}")

        ok = run([sys.executable, os.path.join(HERE, "extract_chapter.py"),
                  "--book", str(args.book), "--chapter", str(num),
                  "--db", db])
        if not ok:
            print(f"  extraction failed for chapter {num}, stopping.")
            sys.exit(1)

        if not args.auto:
            ans = input("  Inspect the JSON, then press Enter to reconcile "
                        "(or 's' to skip, 'q' to quit): ").strip().lower()
            if ans == "q":
                print("Stopped.")
                return
            if ans == "s":
                continue

        rec_cmd = [sys.executable, os.path.join(HERE, "reconcile.py"),
                   "--book", str(args.book), "--chapter", str(num),
                   "--db", db]
        if args.auto:
            rec_cmd.append("--auto")
        run(rec_cmd)

    print("\nDone. Start the web app with:  python app.py")


if __name__ == "__main__":
    main()
