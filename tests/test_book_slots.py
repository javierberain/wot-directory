#!/usr/bin/env python3
"""
Tests for the book slot / label / series_order decoupling (migration 004).

Runs standalone (`python tests/test_book_slots.py`) or under pytest. Uses only
throwaway in-memory / temp-dir SQLite databases; never touches db/.

Covers:
  - directory_rules slot helpers (validate_slot, snapshot_path,
    slot_from_filename, default_order_and_label, resolve_book incl. legacy ints)
  - migration 004 backfill on a pre-migration books table
  - start_book defaults/requirements for bookN vs named slots
  - app.py discovery ordering: newspring (order 11) sorts between book10 and
    book11 (order 12) even though the file names say otherwise
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import directory_rules as dr  # noqa: E402

SCHEMA_PATH = os.path.join(ROOT, "db", "schema.sql")
MIGRATION_004 = os.path.join(ROOT, "db", "migrations", "004_book_slot_label.sql")


def _fresh(path=":memory:"):
    conn = sqlite3.connect(path)
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        conn.executescript(f.read())
    return conn


def _add_book(conn, order, slot, label, title):
    conn.execute(
        "INSERT INTO books (series_order, slot, label, title) VALUES (?, ?, ?, ?)",
        (order, slot, label, title))
    conn.commit()


# ── slot helpers ──────────────────────────────────────────────────────────────

def test_validate_slot():
    for ok in ["book1", "book10", "newspring", "book11"]:
        assert dr.validate_slot(ok) == ok
    for bad in ["Book10", "new spring", "new-spring", "", "10.5", None]:
        try:
            dr.validate_slot(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should be rejected")


def test_snapshot_path_and_filename_roundtrip():
    p = dr.snapshot_path("newspring", db_dir="/x")
    assert os.path.basename(p) == "wot_newspring.db"
    assert dr.slot_from_filename(p) == "newspring"
    assert dr.slot_from_filename("db/wot_book9.db") == "book9"
    for not_snap in ["wot.db", "wot_backup.db", "wot_book1_cleanup.db",
                     "wot_clean_book1.db", "wot_book9.db.post-buggy-commit",
                     "wot_book2.db.pre-origins.bak", "schema.sql"]:
        assert dr.slot_from_filename(not_snap) is None, not_snap


def test_default_order_and_label():
    assert dr.default_order_and_label("book10") == (10, "Book 10")
    assert dr.default_order_and_label("book1") == (1, "Book 1")
    assert dr.default_order_and_label("newspring") == (None, None)


def test_resolve_book_slot_and_legacy_int():
    conn = _fresh()
    _add_book(conn, 10, "book10", "Book 10", "Crossroads of Twilight")
    _add_book(conn, 11, "newspring", "New Spring", "New Spring")
    _add_book(conn, 12, "book11", "Book 11", "Knife of Dreams")
    assert dr.resolve_book(conn, "newspring")["series_order"] == 11
    # legacy integer N means slot bookN, NOT series_order N
    assert dr.resolve_book(conn, 11)["slot"] == "book11"
    assert dr.resolve_book(conn, "11")["title"] == "Knife of Dreams"
    assert dr.resolve_book(conn, "book10")["label"] == "Book 10"
    for missing in ["book12", "99"]:
        try:
            dr.resolve_book(conn, missing)
        except LookupError:
            continue
        raise AssertionError(f"{missing!r} should raise LookupError")
    try:
        dr.resolve_book(conn, "Knife of Dreams")
    except ValueError:
        pass
    else:
        raise AssertionError("malformed slot should raise ValueError")


def test_resolve_book_falls_back_to_series_order_when_slot_absent():
    conn = _fresh()
    _add_book(conn, 10, "book10", "Book 10", "Crossroads of Twilight")
    _add_book(conn, 11, "newspring", "New Spring", "New Spring")
    # no slot 'book11' yet -> integer 11 falls back to series_order 11
    assert dr.resolve_book(conn, 11)["slot"] == "newspring"


# ── migration 004 ─────────────────────────────────────────────────────────────

def test_migration_004_backfills_old_books_table():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE books (
            book_id INTEGER PRIMARY KEY,
            series_order INTEGER NOT NULL UNIQUE,
            title TEXT NOT NULL,
            source_file TEXT);
        INSERT INTO books (series_order, title) VALUES (1, 'The Eye of the World');
        INSERT INTO books (series_order, title) VALUES (9, 'Winter''s Heart');
    """)
    with open(MIGRATION_004, encoding="utf-8") as f:
        conn.executescript(f.read())
    rows = conn.execute(
        "SELECT series_order, slot, label FROM books ORDER BY series_order").fetchall()
    assert rows == [(1, "book1", "Book 1"), (9, "book9", "Book 9")]
    assert dr.resolve_book(conn, 9)["slot"] == "book9"
    # unique index exists
    try:
        conn.execute("INSERT INTO books (series_order, slot, label, title) "
                     "VALUES (10, 'book9', 'Book 10', 'dup')")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("duplicate slot should be rejected")


# ── start_book defaults ───────────────────────────────────────────────────────

def test_start_book_requires_explicit_args_for_named_slots():
    import subprocess
    script = os.path.join(ROOT, "scripts", "start_book.py")
    # a nonexistent epub is fine: argument validation runs first
    r = subprocess.run([sys.executable, script, "--slot", "newspring",
                        "--epub", "nope.epub"], capture_output=True, text=True)
    assert r.returncode != 0
    assert "--order" in r.stderr and "--seed-from" in r.stderr
    r = subprocess.run([sys.executable, script, "--slot", "Book10",
                        "--epub", "nope.epub"], capture_output=True, text=True)
    assert r.returncode != 0 and "bad slot" in (r.stderr + r.stdout)


# ── app.py discovery ordering ─────────────────────────────────────────────────

def _install_flask_stub():
    if "flask" in sys.modules:
        return
    try:
        import flask  # noqa: F401
        return
    except ImportError:
        pass
    stub = types.ModuleType("flask")

    class Flask:
        def __init__(self, *a, **k): pass
        def route(self, *a, **k): return lambda f: f
        def teardown_appcontext(self, f): return f
    stub.Flask = Flask
    stub.g = None
    stub.jsonify = lambda *a, **k: a
    stub.render_template = lambda *a, **k: ""
    stub.request = types.SimpleNamespace(args={})
    sys.modules["flask"] = stub


def test_app_orders_snapshots_by_series_order_not_filename():
    tmp = tempfile.mkdtemp()
    try:
        pub = os.path.join(tmp, "public_db")
        os.makedirs(pub)
        for slot, orders in [("book10", [10]), ("newspring", [10, 11]),
                             ("book11", [10, 11, 12])]:
            conn = _fresh(os.path.join(pub, f"wot_{slot}.db"))
            titles = {10: ("book10", "Book 10", "Crossroads of Twilight"),
                      11: ("newspring", "New Spring", "New Spring"),
                      12: ("book11", "Book 11", "Knife of Dreams")}
            for o in orders:
                _add_book(conn, o, *titles[o])
            conn.close()
        # a scratch DB that must be ignored
        open(os.path.join(pub, "wot.db"), "wb").close()

        _install_flask_stub()
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "app_under_test", os.path.join(ROOT, "app.py"))
        mod = importlib.util.module_from_spec(spec)
        # point the module at the temp public_db BEFORE executing it
        src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
        src = src.replace(
            '_PUBLIC_DB_DIR = os.path.join(os.path.dirname(__file__), "public_db")',
            f'_PUBLIC_DB_DIR = {pub!r}')
        assert f"_PUBLIC_DB_DIR = {pub!r}" in src
        exec(compile(src, "app.py", "exec"), mod.__dict__)

        assert mod.AVAILABLE_BOOKS == ["book10", "newspring", "book11"]
        info = mod.AVAILABLE_BOOKS_INFO
        assert [b["label"] for b in info] == ["Book 10", "New Spring", "Book 11"]
        assert [b["series_order"] for b in info] == [10, 11, 12]

        mod.request = types.SimpleNamespace(args={"book": "11"})
        assert mod.selected_book() == "book11"      # legacy int = slot bookN
        mod.request = types.SimpleNamespace(args={"book": "newspring"})
        assert mod.selected_book() == "newspring"
        mod.request = types.SimpleNamespace(args={"book": "nonsense"})
        assert mod.selected_book() == "book10"      # fails closed to earliest
        mod.request = types.SimpleNamespace(args={})
        assert mod.selected_book() == "book10"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok    {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
