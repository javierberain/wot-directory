#!/usr/bin/env python3
"""
start_book.py - Seed the per-book working snapshot for a new book.

The pipeline is snapshot-based: book N is parsed, reconciled, and cleaned
directly in its own private snapshot db/wot_book{N}.db, which is seeded from the
previous CLEANED snapshot db/wot_book{N-1}.db. This is the step that was done by
hand for book 4 (rebuild book 4 on the book-3 snapshot); it is now scripted so
the legacy scratch db/wot.db is never the base.

What it does:
  1. (N>=2) copy db/wot_book{N-1}.db -> db/wot_book{N}.db  (carries the cleaned
     roster, aliases, factions, mentions, full_text of books 1..N-1).
     (N==1) create a fresh db/wot_book1.db from db/schema.sql.
  2. parse the EPUB into db/wot_book{N}.db (chapters land with extracted=0).
  3. print the next command.

Refuses to overwrite an existing db/wot_book{N}.db unless --force (which backs
the existing file up first).

    python scripts/start_book.py --book 5 \
        --epub "books/The Fires of Heaven.epub" --title "The Fires of Heaven"
    python scripts/run_book.py --book 5            # then ingest
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(__file__)
DB_DIR = os.path.join(HERE, "..", "db")
SCHEMA_PATH = os.path.join(DB_DIR, "schema.sql")

sys.path.insert(0, HERE)
import parse_epub  # noqa: E402


def snapshot_path(n):
    return os.path.join(DB_DIR, f"wot_book{n}.db")


def main():
    ap = argparse.ArgumentParser(
        description="Seed db/wot_book{N}.db from the previous snapshot and "
                    "parse the new book's EPUB into it.")
    ap.add_argument("--book", type=int, required=True, help="series order N")
    ap.add_argument("--epub", required=True, help="path to the .epub file")
    ap.add_argument("--title", help="override book title")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing db/wot_book{N}.db (backed up first)")
    args = ap.parse_args()

    n = args.book
    target = snapshot_path(n)
    if not os.path.exists(args.epub):
        sys.exit(f"EPUB not found: {args.epub}")

    if os.path.exists(target):
        if not args.force:
            sys.exit(f"{target} already exists. Use --force to overwrite "
                     f"(it will be backed up first).")
        bak = target + ".pre-start-book.bak"
        shutil.copy2(target, bak)
        print(f"Backed up existing snapshot -> {bak}")
        os.remove(target)

    # 1. seed
    if n >= 2:
        prev = snapshot_path(n - 1)
        if not os.path.exists(prev):
            sys.exit(f"Previous snapshot not found: {prev}\n"
                     f"Book {n} must be seeded from the cleaned book {n-1} "
                     f"snapshot.")
        shutil.copy2(prev, target)
        print(f"Seeded {os.path.basename(target)} from "
              f"{os.path.basename(prev)}")
    else:
        # parse_book -> get_db(target) creates a fresh DB from schema.sql.
        print(f"Book 1: creating a fresh {os.path.basename(target)} from schema")

    # 2. parse the EPUB into the snapshot
    print(f"Parsing EPUB into {os.path.basename(target)} ...")
    parse_epub.parse_book(args.epub, n, args.title, db_path=target)

    # 3. next step
    print(f"\nDone. Next:\n  python scripts/run_book.py --book {n} --from 0 "
          f"--to 1   # sanity-check, then --auto for the rest")


if __name__ == "__main__":
    main()
