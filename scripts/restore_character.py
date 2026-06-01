#!/usr/bin/env python3
"""
restore_character.py - Copy a single character and all their dependent rows
from a source database into a target database, translating cross-db ids.

Use this when a character was deleted from a snapshot that was later found to
be a real character.  The --src database must still contain the character;
--target is the database to restore into.

Identity translation
  character_id  : per-db, never reused.  The restored character receives a
                  new id assigned by the target db (INTEGER PRIMARY KEY).
  chapter_id    : per-db; translated by matching (book series_order,
                  chapter_number) between source and target.
  faction_id    : per-db; translated by matching the stored name_norm.
  partner char  : relationship partners are looked up by primary_name.

  Optional first_chapter_id fields on aliases, character_factions, and
  relationships are translated the same way; if the referenced chapter is
  absent from target they are set NULL (graceful degradation — cosmetic
  tracking field, not critical).

Safety envelope
  • --src is opened strictly read-only (SQLite URI mode=ro).
  • --target is only written to after an explicit --commit flag.
  • A backup of --target is written before any write, with the same
    WAL/SHM sidecar pattern used by other scripts in this repo.
  • Refuses if the character already exists in --target by primary_name.
  • Refuses to proceed if any external reference (chapter for appearances,
    faction, relationship partner) is MISSING from --target.
    Does NOT auto-create missing rows — that is a separate decision.
  • All writes happen inside a single transaction.  Any error rolls back.
  • Post-write verification re-queries counts and reports discrepancies.
  • Re-running with --commit after a successful restore hits the
    "already present" guard (step 3) and refuses — idempotent by design.

Usage:
    python scripts/restore_character.py \\
        --src  db/wot.db.book2-precleanup.bak \\
        --target db/wot_book2.db \\
        --name "the scar-faced man"

    python scripts/restore_character.py \\
        --src  db/wot.db.book2-precleanup.bak \\
        --target db/wot_book2.db \\
        --name "the scar-faced man" --commit

    --src and --target are both required (no defaults — cross-db restore is
    always a deliberate operation).
    --commit writes to --target.  Without it the script is a dry-run only.
"""

import argparse
import pathlib
import re
import shutil
import sqlite3
import sys


# ── Formatting helpers ────────────────────────────────────────────────────────
_SEP  = "-" * 70
_SEP2 = "=" * 70


def _header(title):
    print(f"\n{title:-<70}")


# ── Text normalisation ────────────────────────────────────────────────────────

def norm(text):
    """Lowercase + smart-apostrophe → straight + collapse whitespace.

    Mirrors the convention used for aliases.alias_norm and factions.name_norm
    in the schema.  Used here for faction-name matching.
    """
    text = text.lower()
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ── Database helpers ──────────────────────────────────────────────────────────

def open_src(path):
    """Open the source database strictly read-only via SQLite URI mode=ro."""
    p = pathlib.Path(path).resolve()
    if not p.exists():
        sys.exit(f"ERROR: source database not found: {p}")
    uri = p.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        sys.exit(f"ERROR: cannot open source db read-only: {exc}\n  {p}")
    conn.row_factory = sqlite3.Row
    return conn


def open_target(path):
    """Open the target database read-write with foreign-key enforcement."""
    p = pathlib.Path(path).resolve()
    if not p.exists():
        sys.exit(f"ERROR: target database not found: {p}")
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def take_backup(target_path, char_name):
    """Copy --target to <target>.pre-restore-<slug>.bak before any writes.

    Copies WAL/SHM sidecar files if they exist, preserving in-flight state.
    """
    p    = pathlib.Path(target_path).resolve()
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", char_name)[:40].strip("-")
    bak  = pathlib.Path(str(p) + f".pre-restore-{slug}.bak")
    shutil.copy2(p, bak)
    for ext in ("-wal", "-shm"):
        sidecar = pathlib.Path(str(p) + ext)
        if sidecar.exists():
            shutil.copy2(sidecar, pathlib.Path(str(bak) + ext))
            print(f"  (backed up WAL sidecar {sidecar.name})")
    print(f"  Backup written: {bak}")


# ── Gather dependent rows from source ─────────────────────────────────────────

def gather_aliases(src, cid):
    """Return all alias rows for cid, ordered primary-first."""
    return src.execute(
        """SELECT alias_id, character_id, alias_text, alias_norm,
                  alias_type, is_primary, notes, first_chapter_id
             FROM aliases
            WHERE character_id = ?
            ORDER BY is_primary DESC, alias_type, alias_text""",
        (cid,),
    ).fetchall()


def gather_appearances(src, cid):
    """Return appearances joined with chapter+book for cross-db translation.

    We carry (series_order, chapter_number) rather than the source chapter_id
    because chapter_ids are per-db and cannot be reused in target.
    """
    return src.execute(
        """SELECT ap.name_used,
                  ap.whereabouts,
                  ap.notable_actions,
                  ap.alliances_shown,
                  ap.demeanor,
                  b.series_order,
                  ch.chapter_number,
                  ch.title        AS chapter_title
             FROM appearances ap
             JOIN chapters ch ON ch.chapter_id = ap.chapter_id
             JOIN books    b  ON  b.book_id    = ch.book_id
            WHERE ap.character_id = ?
            ORDER BY b.series_order, ch.chapter_number""",
        (cid,),
    ).fetchall()


def gather_relationships(src, cid):
    """Return relationships, enriched with the other party's primary_name.

    For each row we record our_side ('a' or 'b') so the character_a / character_b
    position can be reproduced exactly in target.  This preserves:
      - directed semantics (character_a is the source of a directed edge)
      - any lo/hi ordering convention on undirected edges
    """
    return src.execute(
        """SELECT r.relationship_id,
                  r.relationship_type,
                  r.directed,
                  r.description,
                  r.first_chapter_id                          AS src_first_chapter_id,
                  CASE WHEN r.character_a = ? THEN 'a'
                       ELSE 'b' END                           AS our_side,
                  c.primary_name                              AS other_name
             FROM relationships r
             JOIN characters c ON c.character_id =
                  (CASE WHEN r.character_a = ? THEN r.character_b
                        ELSE r.character_a END)
            WHERE r.character_a = ? OR r.character_b = ?
            ORDER BY r.relationship_type, c.primary_name""",
        (cid, cid, cid, cid),
    ).fetchall()


def gather_factions(src, cid):
    """Return character_factions joined with faction metadata."""
    return src.execute(
        """SELECT cf.role,
                  cf.first_chapter_id   AS src_first_chapter_id,
                  cf.notes,
                  f.name                AS faction_name,
                  f.name_norm           AS faction_name_norm,
                  f.faction_type
             FROM character_factions cf
             JOIN factions f ON f.faction_id = cf.faction_id
            WHERE cf.character_id = ?
            ORDER BY f.name""",
        (cid,),
    ).fetchall()


# ── Cross-db id helpers ───────────────────────────────────────────────────────

def translate_chapter_id(src_conn, tgt_conn, src_chapter_id):
    """Translate a src chapter_id to a target chapter_id via (series_order,
    chapter_number) coordinates.  Returns None if either end is absent.

    Used for the optional first_chapter_id tracking fields on aliases,
    character_factions, and relationships.  These are cosmetic (used for
    display only) so a None return is a graceful degradation, not an error.
    """
    if src_chapter_id is None:
        return None
    coords = src_conn.execute(
        """SELECT b.series_order, ch.chapter_number
             FROM chapters ch
             JOIN books b ON b.book_id = ch.book_id
            WHERE ch.chapter_id = ?""",
        (src_chapter_id,),
    ).fetchone()
    if coords is None:
        return None
    row = tgt_conn.execute(
        """SELECT ch.chapter_id
             FROM chapters ch
             JOIN books b ON b.book_id = ch.book_id
            WHERE b.series_order = ? AND ch.chapter_number = ?""",
        (coords["series_order"], coords["chapter_number"]),
    ).fetchone()
    return row["chapter_id"] if row else None


def lookup_chapter_target(tgt, series_order, chapter_number):
    """Return (chapter_id, title) from target by (series_order, chapter_number)."""
    return tgt.execute(
        """SELECT ch.chapter_id, ch.title
             FROM chapters ch
             JOIN books b ON b.book_id = ch.book_id
            WHERE b.series_order = ? AND ch.chapter_number = ?""",
        (series_order, chapter_number),
    ).fetchone()


def lookup_faction_target(tgt, name_norm):
    """Return faction_id from target by normalised faction name, or None."""
    row = tgt.execute(
        "SELECT faction_id FROM factions WHERE name_norm = ?",
        (name_norm,),
    ).fetchone()
    return row["faction_id"] if row else None


def lookup_character_target(tgt, primary_name):
    """Return character_id from target by primary_name, or None."""
    row = tgt.execute(
        "SELECT character_id FROM characters WHERE primary_name = ?",
        (primary_name,),
    ).fetchone()
    return row["character_id"] if row else None


# ── Dossier printing ──────────────────────────────────────────────────────────

def print_dossier(char, aliases, appearances, relationships, factions):
    """Print the full source-side dossier of the character being restored."""

    _header("CHARACTER ROW")
    # Stable-trait fields defined in schema.sql (omit auto-generated ids and
    # timestamps — these will be freshly assigned in target).
    trait_fields = [
        "character_id", "primary_name", "character_type", "nationality",
        "physical_traits", "age", "filiations", "associations",
        "personality", "description", "first_chapter_id",
    ]
    for f in trait_fields:
        try:
            val = char[f]
        except IndexError:
            val = "(column absent in source)"
        null_marker = "  [NULL]" if val is None else ""
        print(f"  {f:<20} : {val!r}{null_marker}")

    _header(f"ALIASES  ({len(aliases)})")
    if not aliases:
        print("  (none)")
    for a in aliases:
        primary_marker = "  [PRIMARY]" if a["is_primary"] else ""
        print(f"  alias_id={a['alias_id']}  \"{a['alias_text']}\"  "
              f"[{a['alias_type']}]{primary_marker}")
        if a["notes"]:
            print(f"    notes      : {a['notes']!r}")
        if a["first_chapter_id"] is not None:
            print(f"    first_ch_id: {a['first_chapter_id']} (src)")

    _header(f"APPEARANCES  ({len(appearances)})")
    if not appearances:
        print("  (none)")
    for ap in appearances:
        print(f"  Bk{ap['series_order']} Ch{ap['chapter_number']:>3}  "
              f"\"{ap['chapter_title']}\"")
        for field in ("name_used", "whereabouts", "notable_actions",
                      "alliances_shown", "demeanor"):
            val = ap[field]
            if val:
                print(f"    {field:<18}: {val!r}")

    _header(f"RELATIONSHIPS  ({len(relationships)})")
    if not relationships:
        print("  (none)")
    for r in relationships:
        directed_str = "directed" if r["directed"] else "undirected"
        our_col = "character_a" if r["our_side"] == "a" else "character_b"
        print(f"  [{r['relationship_type']} / {directed_str}]  "
              f"restored char is {our_col}")
        print(f"    other party: \"{r['other_name']}\"")
        if r["description"]:
            print(f"    description: {r['description']!r}")

    _header(f"FACTION MEMBERSHIPS  ({len(factions)})")
    if not factions:
        print("  (none)")
    for cf in factions:
        print(f"  \"{cf['faction_name']}\"  [{cf['faction_type']}]  "
              f"role={cf['role']!r}")
        if cf["notes"]:
            print(f"    notes: {cf['notes']!r}")


# ── Cross-db lookup check ─────────────────────────────────────────────────────

def check_lookups(tgt, appearances, relationships, factions):
    """Verify every external reference resolves in --target.

    External references:
      • chapters       — by (series_order, chapter_number) for appearances
      • partners       — by primary_name for relationship other-parties
      • factions       — by name_norm for character_factions

    Returns:
      ok           : bool — False if any reference is MISSING IN TARGET
      chapter_map  : {(series_order, chapter_number) -> target_chapter_id}
      faction_map  : {name_norm -> target_faction_id}
      partner_map  : {primary_name -> target_character_id}

    All three maps contain only resolved entries; MISSING entries are not
    included (the caller halts before using them).
    """
    _header("CROSS-DB LOOKUP CHECK")
    ok          = True
    chapter_map = {}
    faction_map = {}
    partner_map = {}

    # ── Chapters (for appearances) ────────────────────────────────────────────
    print(f"\n  {'Chapters':}")
    if not appearances:
        print("    (no appearances to check)")
    for ap in appearances:
        key = (ap["series_order"], ap["chapter_number"])
        if key in chapter_map:
            continue
        row = lookup_chapter_target(tgt, ap["series_order"], ap["chapter_number"])
        if row:
            chapter_map[key] = row["chapter_id"]
            print(f"    Bk{ap['series_order']} Ch{ap['chapter_number']:>3}  "
                  f"\"{ap['chapter_title']}\"  →  OK "
                  f"(target chapter_id={row['chapter_id']})")
        else:
            ok = False
            print(f"    Bk{ap['series_order']} Ch{ap['chapter_number']:>3}  "
                  f"\"{ap['chapter_title']}\"  →  MISSING IN TARGET")

    # ── Relationship partners ─────────────────────────────────────────────────
    print(f"\n  {'Relationship partners':}")
    if not relationships:
        print("    (no relationships to check)")
    for r in relationships:
        name = r["other_name"]
        if name in partner_map:
            continue
        cid = lookup_character_target(tgt, name)
        if cid is not None:
            partner_map[name] = cid
            print(f"    \"{name}\"  →  OK  (target character_id={cid})")
        else:
            ok = False
            print(f"    \"{name}\"  →  MISSING IN TARGET")

    # ── Factions ──────────────────────────────────────────────────────────────
    print(f"\n  {'Factions':}")
    if not factions:
        print("    (no faction memberships to check)")
    for cf in factions:
        n_norm = cf["faction_name_norm"]
        if n_norm in faction_map:
            continue
        fid = lookup_faction_target(tgt, n_norm)
        if fid is not None:
            faction_map[n_norm] = fid
            print(f"    \"{cf['faction_name']}\"  [{cf['faction_type']}]  "
                  f"→  OK  (target faction_id={fid})")
        else:
            ok = False
            print(f"    \"{cf['faction_name']}\"  [{cf['faction_type']}]  "
                  f"→  MISSING IN TARGET")

    return ok, chapter_map, faction_map, partner_map


# ── Restore (commit path only) ────────────────────────────────────────────────

def do_restore(src, tgt, char, aliases, appearances, relationships, factions,
               chapter_map, faction_map, partner_map):
    """Insert all rows for the restored character inside the caller's transaction.

    Returns (new_character_id, counts_dict).
    The caller is responsible for commit/rollback.
    """
    # ── a. INSERT characters ──────────────────────────────────────────────────
    # Translate the optional first_chapter_id tracking field.
    new_first_ch = translate_chapter_id(src, tgt, char["first_chapter_id"])

    tgt.execute(
        """INSERT INTO characters
               (primary_name, character_type, nationality, physical_traits,
                age, filiations, associations, personality, description,
                first_chapter_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            char["primary_name"],
            char["character_type"],
            char["nationality"],
            char["physical_traits"],
            char["age"],
            char["filiations"],
            char["associations"],
            char["personality"],
            char["description"],
            new_first_ch,
        ),
    )
    new_cid = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]

    # ── b. INSERT aliases ─────────────────────────────────────────────────────
    n_aliases = 0
    for a in aliases:
        new_fc = translate_chapter_id(src, tgt, a["first_chapter_id"])
        tgt.execute(
            """INSERT INTO aliases
                   (character_id, alias_text, alias_norm, alias_type,
                    is_primary, notes, first_chapter_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                new_cid,
                a["alias_text"],
                a["alias_norm"],
                a["alias_type"],
                a["is_primary"],
                a["notes"],
                new_fc,
            ),
        )
        n_aliases += 1

    # ── c. INSERT appearances ─────────────────────────────────────────────────
    # chapter_map is guaranteed to contain every key (lookup check passed).
    n_appearances = 0
    for ap in appearances:
        key            = (ap["series_order"], ap["chapter_number"])
        tgt_chapter_id = chapter_map[key]
        tgt.execute(
            """INSERT INTO appearances
                   (character_id, chapter_id, name_used, whereabouts,
                    notable_actions, alliances_shown, demeanor)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                new_cid,
                tgt_chapter_id,
                ap["name_used"],
                ap["whereabouts"],
                ap["notable_actions"],
                ap["alliances_shown"],
                ap["demeanor"],
            ),
        )
        n_appearances += 1

    # ── d. INSERT character_factions ──────────────────────────────────────────
    n_factions = 0
    for cf in factions:
        tgt_faction_id = faction_map[cf["faction_name_norm"]]   # guaranteed
        new_fc         = translate_chapter_id(
            src, tgt, cf["src_first_chapter_id"]
        )
        tgt.execute(
            """INSERT INTO character_factions
                   (character_id, faction_id, role, first_chapter_id, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (
                new_cid,
                tgt_faction_id,
                cf["role"],
                new_fc,
                cf["notes"],
            ),
        )
        n_factions += 1

    # ── e. INSERT relationships ───────────────────────────────────────────────
    # Preserve the character_a / character_b position from the source so that:
    #   - directed semantics are maintained (character_a is the edge source)
    #   - any convention around undirected edge ordering is reproduced
    n_relationships = 0
    for r in relationships:
        tgt_partner_id = partner_map[r["other_name"]]           # guaranteed
        if r["our_side"] == "a":
            char_a, char_b = new_cid, tgt_partner_id
        else:
            char_a, char_b = tgt_partner_id, new_cid
        new_fc = translate_chapter_id(
            src, tgt, r["src_first_chapter_id"]
        )
        tgt.execute(
            """INSERT INTO relationships
                   (character_a, character_b, relationship_type,
                    directed, description, first_chapter_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                char_a,
                char_b,
                r["relationship_type"],
                r["directed"],
                r["description"],
                new_fc,
            ),
        )
        n_relationships += 1

    return new_cid, {
        "aliases":            n_aliases,
        "appearances":        n_appearances,
        "character_factions": n_factions,
        "relationships":      n_relationships,
    }


# ── Post-write verification ───────────────────────────────────────────────────

def verify_restore(tgt, new_cid, expected):
    """Re-query target and confirm all dependent counts match what was inserted.

    Returns a list of discrepancy strings (empty = all good).
    """
    errors = []

    char = tgt.execute(
        "SELECT primary_name FROM characters WHERE character_id = ?",
        (new_cid,),
    ).fetchone()
    if not char:
        return [f"character_id={new_cid} not found in target after commit!"]

    for table, col in [
        ("aliases",            "character_id"),
        ("appearances",        "character_id"),
        ("character_factions", "character_id"),
    ]:
        got  = tgt.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (new_cid,)
        ).fetchone()[0]
        want = expected[table]
        if got != want:
            errors.append(
                f"{table}: inserted {want}, re-queried {got}"
            )

    got_rels = tgt.execute(
        "SELECT COUNT(*) FROM relationships "
        "WHERE character_a = ? OR character_b = ?",
        (new_cid, new_cid),
    ).fetchone()[0]
    want_rels = expected["relationships"]
    if got_rels != want_rels:
        errors.append(
            f"relationships: inserted {want_rels}, re-queried {got_rels}"
        )

    return errors


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=(
            "Restore a deleted character and all their dependent rows from a "
            "source database into a target database, with cross-db id "
            "translation (character_id, chapter_id, faction_id)."
        ),
    )
    ap.add_argument(
        "--src", required=True, metavar="PATH",
        help="Source database that still contains the character (read-only).",
    )
    ap.add_argument(
        "--target", required=True, metavar="PATH",
        help="Target database to restore into (read-write).",
    )
    ap.add_argument(
        "--name", required=True, metavar="NAME",
        help=(
            "primary_name of the character to restore.  "
            "Exact, case-sensitive match against the characters table."
        ),
    )
    ap.add_argument(
        "--commit", action="store_true",
        help=(
            "Write the restored rows to --target.  Without --commit the "
            "script is a dry-run: it prints the full dossier and lookup "
            "results but makes no changes."
        ),
    )
    args = ap.parse_args()

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print()
    print(_SEP2)
    print("  WoT CHARACTER DIRECTORY — RESTORE CHARACTER")
    print(f"  Mode  : {mode}")
    print(f"  Name  : {args.name!r}")
    print(f"  src   : {pathlib.Path(args.src).resolve()}")
    print(f"  target: {pathlib.Path(args.target).resolve()}")
    print(_SEP2)

    # ── Step 1: open databases ────────────────────────────────────────────────
    src = open_src(args.src)
    tgt = open_target(args.target)

    # ── Step 2: find character in src ─────────────────────────────────────────
    char = src.execute(
        "SELECT * FROM characters WHERE primary_name = ?", (args.name,)
    ).fetchone()
    if not char:
        src.close(); tgt.close()
        sys.exit(
            f"\nERROR: {args.name!r} not found in source database.\n"
            f"  {pathlib.Path(args.src).resolve()}"
        )
    src_cid = char["character_id"]

    # ── Step 3: safety — must not already exist in target ─────────────────────
    existing = tgt.execute(
        "SELECT character_id FROM characters WHERE primary_name = ?",
        (args.name,),
    ).fetchone()
    if existing:
        src.close(); tgt.close()
        sys.exit(
            f"\nERROR: {args.name!r} already exists in target "
            f"(character_id={existing['character_id']}).  "
            f"Refusing to duplicate.\n"
            f"(Re-running after a successful restore hits this guard — "
            f"that is the intended idempotency behaviour.)"
        )

    # ── Step 4: gather dependent rows from src ────────────────────────────────
    aliases       = gather_aliases(src, src_cid)
    appearances   = gather_appearances(src, src_cid)
    relationships = gather_relationships(src, src_cid)
    factions      = gather_factions(src, src_cid)

    # ── Step 5: print dossier ─────────────────────────────────────────────────
    print_dossier(char, aliases, appearances, relationships, factions)

    # ── Step 6: cross-db lookup check ─────────────────────────────────────────
    ok, chapter_map, faction_map, partner_map = check_lookups(
        tgt, appearances, relationships, factions
    )

    # ── Step 7: summary ───────────────────────────────────────────────────────
    total_deps = (len(aliases) + len(appearances) +
                  len(relationships) + len(factions))
    _header("SUMMARY")
    print(f"\n  character rows     : 1")
    print(f"  aliases            : {len(aliases)}")
    print(f"  appearances        : {len(appearances)}")
    print(f"  relationships      : {len(relationships)}")
    print(f"  faction memberships: {len(factions)}")
    print(f"  total dependent    : {total_deps}")

    if not ok:
        print()
        print(_SEP)
        print("  HALTED: one or more external references are MISSING IN TARGET.")
        print("  Resolve the missing rows first before retrying.")
        print("  This script does NOT auto-create missing chapters, factions,")
        print("  or relationship partners — that is a separate decision.")
        src.close(); tgt.close()
        sys.exit(1)

    if not args.commit:
        print()
        print(_SEP)
        print("  Dry-run complete.  All lookups passed.  No changes made.")
        print("  Re-run with --commit to write to target.")
        src.close(); tgt.close()
        return

    # ── Step 8: commit ────────────────────────────────────────────────────────
    _header("RESTORING")
    take_backup(args.target, args.name)
    print()

    try:
        new_cid, counts = do_restore(
            src, tgt, char,
            aliases, appearances, relationships, factions,
            chapter_map, faction_map, partner_map,
        )
        tgt.commit()
    except Exception as exc:
        tgt.rollback()
        src.close(); tgt.close()
        sys.exit(
            f"\nERROR during restore: {exc}\n"
            f"All changes rolled back.  Target database is unchanged."
        )

    # ── Post-write verification ───────────────────────────────────────────────
    errors = verify_restore(tgt, new_cid, counts)
    if errors:
        print("  WARNING: post-write verification found discrepancies:")
        for e in errors:
            print(f"    {e}")
        print("  The commit is done but investigate before relying on the data.")
    else:
        print("  Verified: character and all dependent rows present.")

    # ── Step 8 final report ───────────────────────────────────────────────────
    _header("RESTORED")
    print(f"\n  {args.name!r}  →  new character_id={new_cid} in target")
    print(f"  aliases              : {counts['aliases']}")
    print(f"  appearances          : {counts['appearances']}")
    print(f"  relationships        : {counts['relationships']}")
    print(f"  faction memberships  : {counts['character_factions']}")

    src.close()
    tgt.close()


if __name__ == "__main__":
    main()
