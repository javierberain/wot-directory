#!/usr/bin/env python3
"""
start_book.py - Seed the per-book working snapshot for a new book.

The pipeline is snapshot-based: each book is parsed, reconciled, and cleaned in
its own private snapshot db/wot_{slot}.db, seeded from the previous CLEANED
snapshot. Three things that used to be one integer are now separate:

  slot          storage identity -> db/wot_{slot}.db   ('book10', 'newspring', 'book11')
  series_order  READING order sort key                 (10, 11, 12 ...)
  label         short display text on the site        ('Book 10', 'New Spring', 'Book 11')

For slots of the form book{N} the order (N), label ("Book N") and seed
(book{N-1}) are implied. Anything else must spell them out.

    python scripts/start_book.py --slot book10 \
        --epub "books/Book_10_Crossroads_of_Twilight.epub" --title "Crossroads of Twilight"

    python scripts/start_book.py --slot newspring --order 11 --label "New Spring" \
        --seed-from book10 --epub "books/New_Spring.epub" --title "New Spring"

    python scripts/start_book.py --slot book11 --seed-from newspring \
        --epub "books/Book_11_Knife_of_Dreams.epub" --title "Knife of Dreams"
    # (book11 implies order 11, but New Spring holds 11 -> pass --order 12)

What it does:
  1. copy db/wot_{seed_from}.db -> db/wot_{slot}.db (carries the cleaned roster,
     aliases, factions, mentions, full_text of everything read so far).
     The very first book (--seed-from none) creates a fresh DB from schema.sql.
  2. refuse if the seed already contains this slot or an order >= --order.
  3. parse the EPUB into db/wot_{slot}.db (chapters land with extracted=0).
  4. print the next command.

Refuses to overwrite an existing db/wot_{slot}.db unless --force (backed up first).
"""
import argparse
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import parse_epub  # noqa: E402
from directory_rules import (validate_slot, snapshot_path,  # noqa: E402
                             default_order_and_label, BOOK_SLOT_RE)


def main():
    ap = argparse.ArgumentParser(
        description="Seed db/wot_{slot}.db from a previous snapshot and parse "
                    "the new book's EPUB into it.")
    ap.add_argument("--slot", required=True,
                    help="snapshot slot: book10, newspring, book11 ...")
    ap.add_argument("--order", type=int,
                    help="reading-order sort key (default N for slot bookN)")
    ap.add_argument("--label", help="display label (default 'Book N' for slot bookN)")
    ap.add_argument("--seed-from", dest="seed_from",
                    help="slot of the cleaned snapshot to copy from "
                         "(default book{N-1} for slot bookN; 'none' for a fresh DB)")
    ap.add_argument("--epub", required=True, help="path to the .epub file")
    ap.add_argument("--title", help="override book title")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing db/wot_{slot}.db (backed up first)")
    args = ap.parse_args()

    slot = validate_slot(args.slot)
    d_order, d_label = default_order_and_label(slot)
    order = args.order if args.order is not None else d_order
    label = args.label or d_label
    seed_from = args.seed_from
    if seed_from is None and d_order is not None:
        seed_from = f"book{d_order - 1}" if d_order >= 2 else "none"
    missing = [n for n, v in (("--order", order), ("--label", label),
                              ("--seed-from", seed_from)) if v is None]
    if missing:
        ap.error(f"slot {slot!r} is not of the form bookN, so "
                 f"{', '.join(missing)} must be given explicitly")

    target = snapshot_path(slot)
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
    if seed_from.lower() != "none":
        prev = snapshot_path(seed_from)
        if not os.path.exists(prev):
            sys.exit(f"Seed snapshot not found: {prev}\n"
                     f"{slot} must be seeded from a cleaned snapshot.")
        # 2. sanity checks against the seed BEFORE copying anything
        conn = sqlite3.connect(prev)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(books)")}
            if "slot" not in cols:
                sys.exit(f"{prev} has no books.slot column. Run\n"
                         f"  python scripts/apply_migration.py --migration 004 --commit")
            rows = conn.execute(
                "SELECT slot, series_order FROM books ORDER BY series_order").fetchall()
        finally:
            conn.close()
        if any(s == slot for s, _ in rows):
            sys.exit(f"Seed {os.path.basename(prev)} already contains slot {slot!r}.")
        max_order = max((o for _, o in rows), default=0)
        if order <= max_order:
            sys.exit(f"--order {order} must be greater than the seed's highest "
                     f"series_order ({max_order}). Reading order is cumulative; "
                     f"e.g. New Spring = 11 after book10, Knife of Dreams = 12.")
        shutil.copy2(prev, target)
        print(f"Seeded {os.path.basename(target)} from {os.path.basename(prev)} "
              f"(seed holds orders {[o for _, o in rows]})")
    else:
        print(f"Creating a fresh {os.path.basename(target)} from schema")

    # 3. parse the EPUB into the snapshot
    print(f"Parsing EPUB into {os.path.basename(target)} as slot={slot} "
          f"order={order} label={label!r} ...")
    parse_epub.parse_book(args.epub, order, args.title, db_path=target,
                          slot=slot, label=label)

    # 4. next step
    print(f"\nDone. Next:\n  python scripts/run_book.py --book {slot} --from 0 "
          f"--to 0   # sanity-check one chapter, then --auto --batch for the rest")


if __name__ == "__main__":
    main()
