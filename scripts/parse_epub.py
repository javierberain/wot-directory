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


def get_db(db_path=None):
    """Open the database, creating it from schema.sql on first run."""
    db_path = db_path or DB_PATH
    first_time = not os.path.exists(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    if first_time:
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            conn.executescript(f.read())
        print(f"  created new database at {db_path}")
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
      clean_title is None when the navMap label carries no title (a bare
      "Chapter 7" or "Prologue"); parse_book then recovers the real title from
      the chapter file's body.
    """
    low = label.lower().strip()
    if low in NON_CHAPTER_LABELS:
        return None
    # Prologue, with or without a title in the label:
    #   "Prologue: Lightnings" -> title "Lightnings"  (titled navMap, book 7)
    #   "Prologue"             -> title None (recover from the body, book 8)
    m = re.match(r"(?i)^prologue\b(?:[\s\u00a0:]+(.*))?$", label)
    if m:
        title = (m.group(1) or "").strip()
        return ("prologue", 0, title or None)
    # Numbered chapter. The title after the number is OPTIONAL, and the pattern
    # is end-anchored so a number must be the whole token or be followed by a
    # separator (never glued to letters, so "1st Age" is not a chapter):
    #   "4      The Gleeman"         bare number + ws/nbsp (books 1-6, split-HTML)
    #   "Chapter 1: High Chasaline"  "Chapter" + number + ":" (book 7, Calibre)
    #   "Chapter 1"                  "Chapter" + number, NO title (book 8, Calibre)
    # This is a strict superset of the previous pattern (which required a
    # separator + title), so books 1-7 labels still match identically.
    m = re.match(r"(?i)^(?:chapter[\s\u00a0]+)?(\d+)(?:[\s\u00a0:]+(.*))?$", label)
    if m:
        title = (m.group(2) or "").strip()
        return ("chapter", int(m.group(1)), title or None)
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


def title_from_chapter_html(raw):
    """Recover a chapter's real title from its HTML body.

    Used only when the navMap label carried no title (e.g. Calibre EPUBs whose
    navPoints are bare "Chapter N" / "Prologue", such as book 8). These EPUBs
    put the number in one heading (<h2 class="h2">CHAPTER 1</h2>) and the title
    in the next (<h2 class="h2b">To Keep the Bargain</h2>). Generically: return
    the text of the first heading element that is NOT itself a chapter/prologue
    marker. Returns None if no distinct title heading is found.
    """
    soup = BeautifulSoup(raw, "html.parser")
    marker = re.compile(r"(?i)^(?:chapter\s*\d*|prologue|epilogue|prelude)$")
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = re.sub(r"\s+", " ", tag.get_text(" ")).strip()
        if not text or marker.match(text):
            continue
        return text
    return None


def parse_book(epub_path, series_order, title_override=None, db_path=None):
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
        first_raw = None
        for h in hrefs:
            try:
                raw = zf.read(h).decode("utf-8", errors="ignore")
            except KeyError:
                continue
            if first_raw is None:
                first_raw = raw
            parts.append(html_to_text(raw))
        body = "\n".join(p for p in parts if p).strip()
        # The first line of body is often the bare "CHAPTER N" header.
        body = re.sub(r"^CHAPTER\s+\d+\s*", "", body, flags=re.I).strip()
        # When the navMap label had no title (bare "Chapter N" / "Prologue",
        # e.g. book 8's Calibre EPUB), recover the real title from the chapter
        # file's body. Books 1-7 always carry a title in the label, so this path
        # never runs for them.
        if not clean_title and first_raw is not None:
            clean_title = title_from_chapter_html(first_raw)
        if not clean_title:
            clean_title = "Prologue" if number == 0 else f"Chapter {number}"
        parsed.append((number, clean_title, body))

    book_title = title_override or os.path.splitext(
        os.path.basename(epub_path))[0]

    # Write to the database.
    conn = get_db(db_path)
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
    ap.add_argument("--db", help="target database (default: db/wot.db)")
    args = ap.parse_args()

    if not os.path.exists(args.epub):
        sys.exit(f"File not found: {args.epub}")
    parse_book(args.epub, args.order, args.title, db_path=args.db)


if __name__ == "__main__":
    main()
