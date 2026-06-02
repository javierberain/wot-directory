#!/usr/bin/env python3
"""
export_public_dbs.py - Build sanitized public SQLite snapshots.

This script copies the public website data from private book snapshots into
public_db/, omitting raw chapter text, review_queue, extraction artifacts, and
local source paths.

Usage:
    python scripts/export_public_dbs.py

Inputs:
    db/wot_book1.db
    db/wot_book2.db
    db/wot_book3.db

Outputs:
    public_db/wot_book1.db
    public_db/wot_book2.db
    public_db/wot_book3.db
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = ROOT / "db"
DEFAULT_OUTPUT_DIR = ROOT / "public_db"
SNAPSHOTS = ("wot_book1.db", "wot_book2.db", "wot_book3.db", "wot_book4.db")


PUBLIC_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE books (
    book_id       INTEGER PRIMARY KEY,
    series_order  INTEGER NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    source_file   TEXT
);

CREATE TABLE chapters (
    chapter_id      INTEGER PRIMARY KEY,
    book_id         INTEGER NOT NULL REFERENCES books(book_id),
    chapter_number  INTEGER NOT NULL,
    title           TEXT NOT NULL,
    extracted       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (book_id, chapter_number)
);

CREATE TABLE characters (
    character_id     INTEGER PRIMARY KEY,
    primary_name     TEXT NOT NULL UNIQUE,
    display_name     TEXT,
    character_type   TEXT NOT NULL DEFAULT 'human',
    nationality      TEXT,
    physical_traits  TEXT,
    age              TEXT,
    filiations       TEXT,
    personality      TEXT,
    description      TEXT
);

CREATE TABLE aliases (
    character_id  INTEGER NOT NULL REFERENCES characters(character_id),
    alias_text    TEXT NOT NULL,
    alias_norm    TEXT NOT NULL,
    alias_type    TEXT NOT NULL DEFAULT 'nickname',
    is_primary    INTEGER NOT NULL DEFAULT 0,
    notes         TEXT,
    UNIQUE (character_id, alias_norm)
);
CREATE INDEX idx_alias_norm ON aliases(alias_norm);

CREATE TABLE appearances (
    character_id     INTEGER NOT NULL REFERENCES characters(character_id),
    chapter_id       INTEGER NOT NULL REFERENCES chapters(chapter_id),
    name_used        TEXT,
    whereabouts      TEXT,
    notable_actions  TEXT,
    alliances_shown  TEXT,
    demeanor         TEXT,
    UNIQUE (character_id, chapter_id)
);
CREATE INDEX idx_app_chapter ON appearances(chapter_id);
CREATE INDEX idx_app_char ON appearances(character_id);

CREATE TABLE mentions (
    character_id  INTEGER NOT NULL REFERENCES characters(character_id),
    chapter_id    INTEGER NOT NULL REFERENCES chapters(chapter_id),
    name_used     TEXT,
    context       TEXT,
    UNIQUE (character_id, chapter_id)
);
CREATE INDEX idx_mention_chapter ON mentions(chapter_id);
CREATE INDEX idx_mention_char ON mentions(character_id);

CREATE TABLE relationships (
    character_a        INTEGER NOT NULL REFERENCES characters(character_id),
    character_b        INTEGER NOT NULL REFERENCES characters(character_id),
    relationship_type  TEXT NOT NULL,
    directed           INTEGER NOT NULL DEFAULT 0,
    description        TEXT,
    UNIQUE (character_a, character_b, relationship_type)
);
CREATE INDEX idx_rel_a ON relationships(character_a);
CREATE INDEX idx_rel_b ON relationships(character_b);

CREATE TABLE factions (
    faction_id    INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    faction_type  TEXT NOT NULL DEFAULT 'other',
    description   TEXT
);

CREATE TABLE character_factions (
    character_id  INTEGER NOT NULL REFERENCES characters(character_id),
    faction_id    INTEGER NOT NULL REFERENCES factions(faction_id),
    role          TEXT NOT NULL DEFAULT 'member',
    notes         TEXT,
    PRIMARY KEY (character_id, faction_id)
);
CREATE INDEX idx_cf_character ON character_factions(character_id);
CREATE INDEX idx_cf_faction ON character_factions(faction_id);
"""


PUBLIC_TABLES = (
    "books",
    "chapters",
    "characters",
    "aliases",
    "appearances",
    "mentions",
    "relationships",
    "factions",
    "character_factions",
)

LOCAL_PATH_MARKERS = (
    "C:\\",
    "/home/",
    "/Users/",
    "OneDrive",
    "wot-directory",
    "books/",
    "books\\",
)


COPY_STATEMENTS = (
    (
        "books",
        """
        INSERT INTO books (book_id, series_order, title, source_file)
        SELECT book_id, series_order, title, NULL
        FROM private.books
        """,
    ),
    (
        "chapters",
        """
        INSERT INTO chapters (chapter_id, book_id, chapter_number, title, extracted)
        SELECT chapter_id, book_id, chapter_number, title, extracted
        FROM private.chapters
        """,
    ),
    (
        "aliases",
        """
        INSERT INTO aliases (
            character_id, alias_text, alias_norm, alias_type, is_primary, notes
        )
        SELECT character_id, alias_text, alias_norm, alias_type, is_primary, notes
        FROM private.aliases
        """,
    ),
    (
        "appearances",
        """
        INSERT INTO appearances (
            character_id, chapter_id, name_used, whereabouts,
            notable_actions, alliances_shown, demeanor
        )
        SELECT
            character_id, chapter_id, name_used, whereabouts,
            notable_actions, alliances_shown, demeanor
        FROM private.appearances
        """,
    ),
    (
        "mentions",
        """
        INSERT INTO mentions (character_id, chapter_id, name_used, context)
        SELECT character_id, chapter_id, name_used, context
        FROM private.mentions
        """,
    ),
    (
        "relationships",
        """
        INSERT INTO relationships (
            character_a, character_b, relationship_type, directed, description
        )
        SELECT character_a, character_b, relationship_type, directed, description
        FROM private.relationships
        """,
    ),
    (
        "factions",
        """
        INSERT INTO factions (faction_id, name, faction_type, description)
        SELECT faction_id, name, faction_type, description
        FROM private.factions
        """,
    ),
    (
        "character_factions",
        """
        INSERT INTO character_factions (character_id, faction_id, role, notes)
        SELECT character_id, faction_id, role, notes
        FROM private.character_factions
        """,
    ),
)


def quote_path_for_sqlite(path: pathlib.Path) -> str:
    return str(path).replace("'", "''")


def table_count(conn: sqlite3.Connection, db_name: str, table: str) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM "{db_name}"."{table}"').fetchone()[0]


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def text_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cols = []
    for row in conn.execute(f'PRAGMA table_info("{table}")'):
        col_name = row[1]
        col_type = (row[2] or "").upper()
        if "TEXT" in col_type:
            cols.append(col_name)
    return cols


def export_one(input_path: pathlib.Path, output_path: pathlib.Path) -> None:
    if not input_path.exists():
        raise FileNotFoundError(f"Missing input database: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    conn = sqlite3.connect(tmp_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(PUBLIC_SCHEMA)
        conn.execute(f"ATTACH DATABASE '{quote_path_for_sqlite(input_path)}' AS private")

        private_character_cols = table_columns(conn, "private.characters")
        display_name_expr = (
            "display_name" if "display_name" in private_character_cols else "primary_name"
        )
        conn.execute(
            f"""
            INSERT INTO characters (
                character_id, primary_name, display_name, character_type,
                nationality, physical_traits, age, filiations, personality,
                description
            )
            SELECT
                character_id, primary_name, {display_name_expr}, character_type,
                nationality, physical_traits, age, filiations, personality,
                description
            FROM private.characters
            """
        )

        for table, sql in COPY_STATEMENTS:
            if table == "characters":
                continue
            conn.execute(sql)

        conn.commit()
        conn.execute("DETACH DATABASE private")
        conn.close()

        if output_path.exists():
            output_path.unlink()
        tmp_path.replace(output_path)
    except Exception:
        conn.close()
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def verify_one(input_path: pathlib.Path, output_path: pathlib.Path) -> list[str]:
    errors: list[str] = []
    conn = sqlite3.connect(output_path)
    try:
        conn.execute(f"ATTACH DATABASE '{quote_path_for_sqlite(input_path)}' AS private")

        for table in PUBLIC_TABLES:
            private_count = table_count(conn, "private", table)
            public_count = table_count(conn, "main", table)
            if private_count != public_count:
                errors.append(
                    f"{output_path.name}: {table} row count mismatch "
                    f"private={private_count} public={public_count}"
                )

        chapter_cols = table_columns(conn, "chapters")
        if "full_text" in chapter_cols:
            errors.append(f"{output_path.name}: chapters.full_text is present")

        if has_table(conn, "review_queue"):
            errors.append(f"{output_path.name}: review_queue table is present")

        book_cols = table_columns(conn, "books")
        if "source_file" in book_cols:
            non_null_sources = conn.execute(
                "SELECT COUNT(*) FROM books WHERE source_file IS NOT NULL"
            ).fetchone()[0]
            if non_null_sources:
                errors.append(
                    f"{output_path.name}: books.source_file has non-NULL values"
                )

        if "display_name" not in table_columns(conn, "characters"):
            errors.append(f"{output_path.name}: characters.display_name is missing")

        for table in PUBLIC_TABLES:
            for column in text_columns(conn, table):
                clauses = " OR ".join([f'"{column}" LIKE ?' for _ in LOCAL_PATH_MARKERS])
                params = [f"%{marker}%" for marker in LOCAL_PATH_MARKERS]
                count = conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE {clauses}',
                    params,
                ).fetchone()[0]
                if count:
                    errors.append(
                        f"{output_path.name}: {table}.{column} contains "
                        f"{count} local-path-like value(s)"
                    )

        conn.execute("DETACH DATABASE private")
    finally:
        conn.close()
    return errors


def print_counts(db_path: pathlib.Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        print(f"\n{db_path}")
        for table in PUBLIC_TABLES:
            count = table_count(conn, "main", table)
            print(f"  {table:20s} {count:6d}")
        print("  chapters.full_text   absent")
        print("  review_queue         absent")
        print("  books.source_file    NULL")
        print("  local path markers   absent")
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export sanitized public SQLite snapshots."
    )
    parser.add_argument(
        "--input-dir",
        type=pathlib.Path,
        default=DEFAULT_INPUT_DIR,
        help="Directory containing private wot_bookN.db snapshots.",
    )
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write sanitized public snapshots.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()

    print(f"Input : {input_dir}")
    print(f"Output: {output_dir}")

    all_errors: list[str] = []
    outputs: list[pathlib.Path] = []
    for name in SNAPSHOTS:
        input_path = input_dir / name
        output_path = output_dir / name
        print(f"\nExporting {input_path.name} -> {output_path}")
        export_one(input_path, output_path)
        errors = verify_one(input_path, output_path)
        all_errors.extend(errors)
        outputs.append(output_path)

    if all_errors:
        print("\nVerification failed:")
        for error in all_errors:
            print(f"  ERROR: {error}")
        return 1

    print("\nVerification passed.")
    for output in outputs:
        print_counts(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
