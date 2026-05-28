#!/usr/bin/env python3
"""
parse_epub.py - Extract chapters from a Wheel of Time EPUB into the database.

This parser is tuned to the EPUB layout of the uploaded copies:
  - The book is split into many `index_split_NNN.html` files.
  - `toc.ncx` contains a navMap: each chapter label points to the
    HTML file where that chapter begins.
  - A chapter spans from its own start file up to (not including) the
    next navMap entry's start file.
  - Front/back matter (MAPS, GLOSSARY, etc.) is skipped; only the
    PROLOGUE and numbered chapters are kept.

Usage:
    python parse_epub.py <path-to-epub> --order <series_order> [--title "..."]

Example:
    python parse_epub.py "Eye of the World.epub" --order 1 \
        --title "The Eye of the World"
"""
import argparse
import os
import re
import sqlite3
import sys
import zipfile
import html  

from bs4 import BeautifulSoup

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "wot.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")

# Front/back matter labels that appear in the navMap but are not chapters.
NON_CHAPTER_LABELS = {
    "maps", "map", "glossary", "contents", "cover", "title page",
    "about the author", "copyright", "acknowledgments", "dedication",
    "prologue note", "table of contents",
}


def get_db():
    """Open the database, creating it from schema.sql on first run."""
    first_time = not os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    if first_time:
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            conn.executescript(f.read())
        print(f"  created new database at {DB_PATH}")
    return conn


def find_opf_and_spine(zf):
    """Return (opf_dir, ordered list of href) from the EPUB spine."""
    container = zf.read("META-INF/container.xml").decode("utf-8")
    opf_path = re.search(r'full-path="(.*?)"', container).group(1)
    opf_dir = os.path.dirname(opf_path)
    opf = zf.read(opf_path).decode("utf-8")

    # Map manifest id -> href (attributes can be in any order).
    items = {}
    for tag in re.finditer(r"<item\b[^>]*>", opf):
        t = tag.group(0)
        iid = re.search(r'id="(.*?)"', t)
        href = re.search(r'href="(.*?)"', t)
        if iid and href:
            items[iid.group(1)] = href.group(1)

    spine = []
    for m in re.findall(r'<itemref idref="(.*?)"', opf):
        if m not in items:
            continue
        href = items[m]
        if opf_dir and not href.startswith(opf_dir):
            href = f"{opf_dir}/{href}"
        spine.append(href)
    return opf_dir, spine


def parse_ncx(zf, opf_dir):
    """
    Return an ordered list of (label, href) from toc.ncx.
    href is relative to the EPUB root.
    """
    # Locate the ncx file.
    ncx_name = None
    for name in zf.namelist():
        if name.lower().endswith(".ncx"):
            ncx_name = name
            break
    if not ncx_name:
        raise RuntimeError("No .ncx table of contents found in EPUB.")

    ncx = zf.read(ncx_name).decode("utf-8")
    entries = []
    for m in re.finditer(
        r"<navPoint[^>]*>.*?<text>(.*?)</text>.*?src=\"(.*?)\"", ncx, re.S
    ):
        label = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
        href = m.group(2).split("#")[0]
        if opf_dir and not href.startswith(opf_dir):
            href = f"{opf_dir}/{href}"
        entries.append((label, href))
    return entries


def classify(label):
    """
    Decide if a navMap label is a real chapter.
    Returns (kind, number, clean_title) or None to skip.
      kind is 'prologue' or 'chapter'.
    """
    low = label.lower().strip()
    if low in NON_CHAPTER_LABELS:
        return None
    if low.startswith("prologue"):
        title = label.split(None, 1)[1].strip() if " " in label else "Prologue"
        return ("prologue", 0, title)
    # Numbered chapter: "4      The Gleeman"  (separator is non-breaking space)
    m = re.match(r"^(\d+)[\s\u00a0]+(.*)$", label)
    if m:
        return ("chapter", int(m.group(1)), m.group(2).strip())
    return None


def html_to_text(raw):
    """Strip HTML to clean readable prose."""
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    # Collapse whitespace, drop the running header line if present.
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # The EPUB repeats a running title on every file; drop it.
        if line.startswith("The Eye of the World") and "Wheel of Time" in line:
            continue
        lines.append(line)
    return "\n".join(lines)


def parse_book(epub_path, series_order, title_override=None):
    zf = zipfile.ZipFile(epub_path)
    opf_dir, spine = find_opf_and_spine(zf)
    ncx_entries = parse_ncx(zf, opf_dir)

    # Build chapter ranges: each chapter runs from its start href in the
    # spine up to the next navMap entry's start href.
    spine_index = {href: i for i, href in enumerate(spine)}

    chapters = []  # (number, title, [spine slice of hrefs])
    for idx, (label, href) in enumerate(ncx_entries):
        info = classify(label)
        if not info:
            continue
        kind, number, clean_title = info
        start = spine_index.get(href)
        if start is None:
            print(f"  WARNING: {href} not in spine, skipping '{label}'")
            continue
        # End = start of the next navMap entry (whatever it is).
        end = len(spine)
        if idx + 1 < len(ncx_entries):
            nxt = spine_index.get(ncx_entries[idx + 1][1])
            if nxt is not None:
                end = nxt
        hrefs = spine[start:end]
        chapters.append((number, clean_title, hrefs))

    # Stitch text for each chapter.
    parsed = []
    for number, clean_title, hrefs in chapters:
        parts = []
        for h in hrefs:
            try:
                raw = zf.read(h).decode("utf-8", errors="ignore")
            except KeyError:
                continue
            parts.append(html_to_text(raw))
        body = "\n".join(p for p in parts if p).strip()
        # The first line of body is often the bare "CHAPTER N" header.
        body = re.sub(r"^CHAPTER\s+\d+\s*", "", body, flags=re.I).strip()
        parsed.append((number, clean_title, body))

    book_title = title_override or os.path.splitext(
        os.path.basename(epub_path))[0]

    # Write to the database.
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO books (series_order, title, source_file) "
        "VALUES (?, ?, ?)",
        (series_order, book_title, os.path.basename(epub_path)),
    )
    cur.execute("SELECT book_id FROM books WHERE series_order = ?",
                (series_order,))
    book_id = cur.fetchone()[0]

    inserted = 0
    for number, clean_title, body in parsed:
        wc = len(body.split())
        cur.execute(
            "INSERT OR IGNORE INTO chapters "
            "(book_id, chapter_number, title, full_text, word_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (book_id, number, clean_title, body, wc),
        )
        if cur.rowcount:
            inserted += 1
    conn.commit()

    print(f"\nBook: {book_title}  (series order {series_order})")
    print(f"Chapters found: {len(parsed)}   newly inserted: {inserted}")
    print(f"{'#':>4}  {'words':>7}  title")
    for number, clean_title, body in parsed:
        label = "PRO" if number == 0 else str(number)
        print(f"{label:>4}  {len(body.split()):>7}  {clean_title}")
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="Parse a WoT EPUB into the DB.")
    ap.add_argument("epub", help="path to the .epub file")
    ap.add_argument("--order", type=int, required=True,
                    help="series order, e.g. 1 for Eye of the World")
    ap.add_argument("--title", help="override book title")
    args = ap.parse_args()

    if not os.path.exists(args.epub):
        sys.exit(f"File not found: {args.epub}")
    parse_book(args.epub, args.order, args.title)


if __name__ == "__main__":
    main()
