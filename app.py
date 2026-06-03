#!/usr/bin/env python3
"""
app.py - Flask web app for the Wheel of Time Character Directory.

Endpoints:
  GET  /                      the single-page UI
  GET  /api/search?q=...      search characters by any name/alias
  GET  /api/character/<id>    full profile + per-chapter appearances
  GET  /api/books             list of books with their chapters
  GET  /api/chapter/<id>      characters featured in one chapter
  GET  /api/faction/<id>      a faction's metadata + member list
  GET  /api/factions          all factions
  GET  /api/graph             relationship graph (optionally ?chapter=ID)

All data endpoints accept ?book=N to select the spoiler boundary.  Missing,
non-numeric, or unavailable values collapse to the EARLIEST available book
(never the latest), so the guard fails closed by design.

Private db/wot*.db files are ingestion/cleanup artifacts and are never served.
The public website reads sanitized public_db/wot_bookN.db snapshots generated
by scripts/export_public_dbs.py.

Run:
    python app.py
    open http://127.0.0.1:5000
"""
import os
import sqlite3

from flask import Flask, g, jsonify, render_template, request

_PUBLIC_DB_DIR = os.path.join(os.path.dirname(__file__), "public_db")
_EXPORT_COMMAND = "python scripts/export_public_dbs.py"

# Sanitized snapshot files served by the web app, auto-discovered from
# public_db/wot_book*.db. Exporting a new book's public snapshot is enough to
# serve it — no code edit needed.
def _discover_books_db():
    import glob as _glob
    import re as _re
    out = {}
    for path in _glob.glob(os.path.join(_PUBLIC_DB_DIR, "wot_book*.db")):
        m = _re.match(r"wot_book(\d+)\.db$", os.path.basename(path))
        if m:
            out[int(m.group(1))] = path
    return out


BOOKS_DB = _discover_books_db()

# Only books whose public snapshot file actually exists on disk can be served.
# Computed once at startup so a missing file can never be served.
AVAILABLE_BOOKS = sorted(n for n, path in BOOKS_DB.items() if os.path.exists(path))
MISSING_PUBLIC_DBS = [
    path for path in BOOKS_DB.values()
    if not os.path.exists(path)
]


def _missing_public_db_message():
    missing = "\n".join(f"  - {path}" for path in MISSING_PUBLIC_DBS)
    return (
        "Missing expected sanitized public database snapshot(s):\n"
        f"{missing}\n"
        f"Run `{_EXPORT_COMMAND}` from the project root to regenerate them."
    )


if MISSING_PUBLIC_DBS:
    raise RuntimeError(_missing_public_db_message())


def _load_available_books_info():
    """Return [{series_order, title}] for every available boundary.

    Queries the largest snapshot so all book titles are reachable in one
    connection.  Result is used to populate the global spoiler selector in
    the HTML template without a round-trip fetch.
    """
    if not AVAILABLE_BOOKS:
        return []
    db_path = BOOKS_DB[max(AVAILABLE_BOOKS)]
    titles = {}
    try:
        conn = sqlite3.connect(db_path)
        for row in conn.execute("SELECT series_order, title FROM books"):
            if row[0] in AVAILABLE_BOOKS:
                titles[row[0]] = row[1]
        conn.close()
    except Exception:
        pass
    return [
        {"series_order": n, "title": titles.get(n, f"Book {n}")}
        for n in AVAILABLE_BOOKS
    ]


# Cached at startup; passed to the template so the selector renders without
# needing to know its own boundary before it has been chosen.
AVAILABLE_BOOKS_INFO = _load_available_books_info()

app = Flask(__name__)


def selected_book():
    """Return the validated book number from ?book=N.

    Fails closed: anything missing, non-numeric, or not in AVAILABLE_BOOKS
    collapses to the EARLIEST available book, never the latest.
    """
    try:
        n = int(request.args.get("book", 0))
    except (ValueError, TypeError):
        n = 0
    if not AVAILABLE_BOOKS or n not in AVAILABLE_BOOKS:
        return AVAILABLE_BOOKS[0] if AVAILABLE_BOOKS else None
    return n


def db():
    """Return the cached SQLite connection for the currently selected book boundary.

    Connections are cached per-book on Flask's g using _db_{N} attributes so
    that a single request can theoretically open multiple boundaries (the
    teardown closes all of them).  In practice each request uses one boundary.
    """
    book = selected_book()
    if book is None:
        raise RuntimeError("No snapshot database files are available.")
    attr = f"_db_{book}"
    if not hasattr(g, attr):
        db_path = BOOKS_DB[book]
        if not os.path.exists(db_path):
            raise RuntimeError(
                "Sanitized public database snapshot is missing at runtime:\n"
                f"  - {db_path}\n"
                f"Run `{_EXPORT_COMMAND}` from the project root to regenerate it."
            )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        setattr(g, attr, conn)
    return getattr(g, attr)


@app.teardown_appcontext
def close_db(exc):
    for book in AVAILABLE_BOOKS:
        conn = getattr(g, f"_db_{book}", None)
        if conn is not None:
            conn.close()


@app.route("/")
def index():
    return render_template(
        "index_codex_v3.html",
        available_books_info=AVAILABLE_BOOKS_INFO,
    )


@app.route("/api/search")
def search():
    q = request.args.get("q", "").strip().lower()
    if not q:
        return jsonify([])

    rows = db().execute("""
        SELECT DISTINCT
            c.character_id,
            c.primary_name,
            c.display_name,
            c.nationality,
            c.description
        FROM characters c
        JOIN aliases a ON a.character_id = c.character_id
        WHERE a.alias_norm LIKE ?
        ORDER BY COALESCE(c.display_name, c.primary_name)
        LIMIT 40
    """, (f"%{q}%",)).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/character/<int:cid>")
def character(cid):
    conn = db()
    char = conn.execute(
        "SELECT * FROM characters WHERE character_id = ?", (cid,)
    ).fetchone()
    if not char:
        return jsonify({"error": "not found"}), 404

    aliases = conn.execute(
        "SELECT alias_text, alias_type, notes FROM aliases "
        "WHERE character_id = ? ORDER BY is_primary DESC, alias_type",
        (cid,),
    ).fetchall()

    appearances = conn.execute("""
        SELECT b.series_order, b.title AS book_title,
               ch.chapter_number, ch.title AS chapter_title,
               ap.whereabouts, ap.notable_actions, ap.alliances_shown,
               ap.demeanor
        FROM appearances ap
        JOIN chapters ch ON ch.chapter_id = ap.chapter_id
        JOIN books b ON b.book_id = ch.book_id
        WHERE ap.character_id = ?
        ORDER BY b.series_order, ch.chapter_number
    """, (cid,)).fetchall()

    # Chapters where the character is referenced but not present (offstage).
    mentions = conn.execute("""
        SELECT b.series_order, b.title AS book_title,
               ch.chapter_number, ch.title AS chapter_title, mn.context
        FROM mentions mn
        JOIN chapters ch ON ch.chapter_id = mn.chapter_id
        JOIN books b ON b.book_id = ch.book_id
        WHERE mn.character_id = ?
        ORDER BY b.series_order, ch.chapter_number
    """, (cid,)).fetchall()

    factions = conn.execute("""
        SELECT f.faction_id, f.name, f.faction_type,
               cf.role, cf.notes
        FROM character_factions cf
        JOIN factions f ON f.faction_id = cf.faction_id
        WHERE cf.character_id = ?
        ORDER BY f.faction_type, f.name
    """, (cid,)).fetchall()

    rels = conn.execute("""
        SELECT r.relationship_type, r.directed, r.description,
               CASE WHEN r.character_a = ? THEN r.character_b
                    ELSE r.character_a END AS other_id
        FROM relationships r
        WHERE r.character_a = ? OR r.character_b = ?
    """, (cid, cid, cid)).fetchall()

    rel_out = []
    for r in rels:
        other = conn.execute(
            "SELECT primary_name FROM characters WHERE character_id = ?",
            (r["other_id"],),
        ).fetchone()
        rel_out.append({
            "type": r["relationship_type"],
            "directed": bool(r["directed"]),
            "description": r["description"],
            "other_id": r["other_id"],
            "other_name": other["primary_name"] if other else "?",
        })

    return jsonify({
        "character": dict(char),
        "aliases": [dict(a) for a in aliases],
        "appearances": [dict(a) for a in appearances],
        "mentions": [dict(m) for m in mentions],
        "relationships": rel_out,
        "factions": [dict(f) for f in factions],
    })


@app.route("/api/factions")
def factions():
    rows = db().execute("""
        SELECT faction_id, name, faction_type
        FROM factions
        ORDER BY name
    """).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/faction/<int:fid>")
def faction(fid):
    conn = db()
    fac = conn.execute(
        "SELECT * FROM factions WHERE faction_id = ?", (fid,)
    ).fetchone()
    if not fac:
        return jsonify({"error": "not found"}), 404
    members = conn.execute("""
        SELECT c.character_id, c.primary_name, c.character_type,
               cf.role, cf.notes
        FROM character_factions cf
        JOIN characters c ON c.character_id = cf.character_id
        WHERE cf.faction_id = ?
        ORDER BY (cf.role = 'leader') DESC, c.primary_name
    """, (fid,)).fetchall()
    return jsonify({
        "faction": dict(fac),
        "members": [dict(m) for m in members],
    })


@app.route("/api/books")
def books():
    conn = db()
    out = []
    for b in conn.execute(
        "SELECT * FROM books ORDER BY series_order"
    ).fetchall():
        chapters = conn.execute("""
            SELECT chapter_id, chapter_number, title, extracted
            FROM chapters WHERE book_id = ?
            ORDER BY chapter_number
        """, (b["book_id"],)).fetchall()
        out.append({
            "book_id": b["book_id"],
            "series_order": b["series_order"],
            "title": b["title"],
            "chapters": [dict(c) for c in chapters],
        })
    return jsonify(out)


@app.route("/api/chapter/<int:chid>")
def chapter(chid):
    conn = db()
    ch = conn.execute("""
        SELECT ch.*, b.title AS book_title, b.series_order
        FROM chapters ch JOIN books b ON b.book_id = ch.book_id
        WHERE ch.chapter_id = ?
    """, (chid,)).fetchone()
    if not ch:
        return jsonify({"error": "not found"}), 404

    chars = conn.execute("""
        SELECT c.character_id, c.primary_name, ap.name_used,
               ap.whereabouts, ap.notable_actions, ap.demeanor
        FROM appearances ap
        JOIN characters c ON c.character_id = ap.character_id
        WHERE ap.chapter_id = ?
        ORDER BY c.primary_name
    """, (chid,)).fetchall()

    return jsonify({
        "chapter": {
            "chapter_id": ch["chapter_id"],
            "chapter_number": ch["chapter_number"],
            "title": ch["title"],
            "book_title": ch["book_title"],
            "extracted": ch["extracted"],
        },
        "characters": [dict(c) for c in chars],
    })


@app.route("/api/graph")
def graph():
    """Relationship graph. ?chapter=ID limits to one chapter's cast."""
    conn = db()
    chapter_id = request.args.get("chapter", type=int)

    if chapter_id:
        char_ids = [r["character_id"] for r in conn.execute(
            "SELECT character_id FROM appearances WHERE chapter_id = ?",
            (chapter_id,),
        ).fetchall()]
    else:
        char_ids = [r["character_id"] for r in conn.execute(
            "SELECT character_id FROM characters"
        ).fetchall()]

    if not char_ids:
        return jsonify({"nodes": [], "edges": []})

    placeholders = ",".join("?" * len(char_ids))
    nodes = conn.execute(f"""
        SELECT c.character_id, c.primary_name, c.nationality,
               c.character_type,
               (SELECT COUNT(*) FROM appearances ap
                WHERE ap.character_id = c.character_id) AS appearances
        FROM characters c
        WHERE c.character_id IN ({placeholders})
    """, char_ids).fetchall()

    # For "colour by faction", each node carries the list of factions it
    # belongs to. The frontend picks one (usually the first ajah/order/
    # house) per node when colouring.
    fac_rows = conn.execute(f"""
        SELECT cf.character_id, f.faction_id, f.name, f.faction_type
        FROM character_factions cf
        JOIN factions f ON f.faction_id = cf.faction_id
        WHERE cf.character_id IN ({placeholders})
    """, char_ids).fetchall()
    fac_by_char = {}
    for r in fac_rows:
        fac_by_char.setdefault(r["character_id"], []).append({
            "faction_id": r["faction_id"],
            "name": r["name"],
            "faction_type": r["faction_type"],
        })

    edges = conn.execute(f"""
        SELECT character_a, character_b, relationship_type, directed,
               description
        FROM relationships
        WHERE character_a IN ({placeholders})
          AND character_b IN ({placeholders})
    """, char_ids + char_ids).fetchall()

    return jsonify({
        "nodes": [{
            "id": n["character_id"],
            "label": n["primary_name"],
            "nationality": n["nationality"],
            "character_type": n["character_type"],
            "appearances": n["appearances"],
            "factions": fac_by_char.get(n["character_id"], []),
        } for n in nodes],
        "edges": [{
            "source": e["character_a"],
            "target": e["character_b"],
            "type": e["relationship_type"],
            "directed": bool(e["directed"]),
            "description": e["description"],
        } for e in edges],
    })


if __name__ == "__main__":
    if not AVAILABLE_BOOKS:
        print("Warning: no sanitized public snapshot database files found in public_db/.")
        print("Expected: public_db/wot_book1.db, public_db/wot_book2.db, ...")
        print(f"Run: {_EXPORT_COMMAND}")
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes", "on"}
    app.run(debug=debug, port=5000)
