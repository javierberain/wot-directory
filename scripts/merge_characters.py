#!/usr/bin/env python3
"""
merge_characters.py - Fold a duplicate character row into a canonical row,
retargeting all dependent data, then delete the duplicate.

Use this when the same real person has ended up as two separate character rows
(e.g. "Siuan Sanche" and "the Amyrlin Seat", or "Else" and "Else Grinwell").
All aliases, appearances, relationships, and faction memberships are moved or
merged; no data on the canonical row is silently overwritten.

Merge behaviour by table
  characters      : stable-trait fields filled from duplicate only when canonical
                    is placeholder (NULL / empty / "unknown...").  Never overwritten.
                    character_type mismatch → HALT (data inconsistency).
  aliases         : duplicate's aliases inserted on canonical, deduped by
                    alias_norm.  Duplicate's own primary_name added as alias_type
                    'epithet' (the closest valid type for a "formerly-primary" label;
                    schema CHECK allows only primary/given_name/title/nickname/
                    disguise/epithet — 'primary' is excluded as specified).
  appearances     : reassigned to canonical unless canonical already has an
                    appearance in the same chapter (conflict → discard duplicate's).
  relationships   : reassigned to canonical; self-loops (other party == canonical)
                    are detected and discarded.  For undirected edges, both storage
                    orderings are checked when looking for a conflict.
  character_factions: reassigned to canonical; duplicate if canonical already has
                    the same faction_id (unique PK constraint: one row per character
                    per faction).

Safety envelope
  • Dry-run by default; --commit required to write.
  • Backup before any write: "<target>.pre-merge-<canonical-slug>.bak".
  • Halt if canonical or duplicate not found, or if either is ambiguous.
  • Halt if canonical == duplicate (same character_id).
  • Halt if character_type differs between canonical and duplicate.
  • Never overwrite a real (non-placeholder) value on the canonical's character row.
  • Alias inserts deduplicated by alias_norm.
  • Self-loop relationships detected and discarded, never inserted.
  • Orphan check before deleting the duplicate: all four dependent tables must
    have zero rows pointing at the duplicate's character_id; if any remain,
    the transaction is rolled back and the script exits with an error.
  • Single transaction with rollback on any error.
  • Idempotent: re-running with --commit after a successful merge finds
    "already present" or "already matches" conditions throughout and writes nothing.

Usage:
    python scripts/merge_characters.py \\
        --target db/wot_book2.db \\
        --canonical "Siuan Sanche" \\
        --duplicate "the Amyrlin Seat"

    python scripts/merge_characters.py \\
        --target db/wot_book2.db \\
        --canonical "Siuan Sanche" \\
        --duplicate "the Amyrlin Seat" \\
        --commit
"""

import argparse
import pathlib
import re
import shutil
import sqlite3
import sys


# ── Formatting ────────────────────────────────────────────────────────────────
_SEP  = "-" * 70
_SEP2 = "=" * 70


def _header(title):
    print(f"\n{title:-<70}")


# ── Text normalisation (mirrors resolve_origins.py exactly) ───────────────────

def norm_for_search(text):
    """Lowercase + smart-apostrophe → straight + collapse whitespace."""
    text = text.lower()
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_placeholder(value):
    """Return True if value is NULL, empty, or starts with 'unknown'.

    Works for both text fields and integer/NULL fields (first_chapter_id):
    None → True, any non-None integer → False.
    """
    if value is None:
        return True
    s = str(value).strip()
    if s == "":
        return True
    return s.lower().startswith("unknown")


# ── Stable-trait field list ───────────────────────────────────────────────────
# These are the mutable fields on the characters row that the merge plan
# considers.  character_id, primary_name, created_at, updated_at are excluded:
# they are never touched during a merge.  character_type is handled separately
# (HALT on mismatch).
_TRAIT_FIELDS = [
    "nationality",
    "physical_traits",
    "age",
    "filiations",
    "associations",
    "personality",
    "description",
    "first_chapter_id",
]

# ── Database helpers ──────────────────────────────────────────────────────────

def open_db(path):
    """Open the target database read-write with foreign-key enforcement."""
    p = pathlib.Path(path).resolve()
    if not p.exists():
        sys.exit(f"ERROR: database not found: {p}")
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def take_backup(target_path, canonical_name):
    """Copy --target to <target>.pre-merge-<slug>.bak before any writes."""
    p    = pathlib.Path(target_path).resolve()
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", canonical_name)[:40].strip("-")
    bak  = pathlib.Path(str(p) + f".pre-merge-{slug}.bak")
    shutil.copy2(p, bak)
    for ext in ("-wal", "-shm"):
        sidecar = pathlib.Path(str(p) + ext)
        if sidecar.exists():
            shutil.copy2(sidecar, pathlib.Path(str(bak) + ext))
            print(f"  (backed up WAL sidecar {sidecar.name})")
    print(f"  Backup written: {bak}")


# ── Character lookup ──────────────────────────────────────────────────────────

def find_character(conn, name, label):
    """Return the character row for primary_name=name, or halt.

    label is "canonical" or "duplicate" for error messages.
    """
    rows = conn.execute(
        "SELECT * FROM characters WHERE primary_name = ?", (name,)
    ).fetchall()
    if len(rows) == 0:
        sys.exit(f"\nERROR: {label} {name!r} not found in target database.")
    if len(rows) > 1:
        # primary_name is UNIQUE so this shouldn't happen, but guard anyway.
        sys.exit(
            f"\nERROR: {label} {name!r} matches {len(rows)} rows "
            f"(primary_name should be UNIQUE — investigate the database)."
        )
    return rows[0]


# ── Plan builders (pure computation — no writes) ──────────────────────────────

def build_trait_plan(canonical, duplicate):
    """Compare stable-trait fields; return a list of plan items.

    Each item is a dict:
        field      : str
        action     : 'keep' | 'fill' | 'both_placeholder'
        can_value  : current canonical value
        dup_value  : duplicate value
        note       : str  (e.g. "duplicate has different non-empty value")
    """
    plan = []
    for field in _TRAIT_FIELDS:
        try:
            can_val = canonical[field]
            dup_val = duplicate[field]
        except IndexError:
            # Column absent in this db (e.g. display_name variant).
            continue

        can_ph = is_placeholder(can_val)
        dup_ph = is_placeholder(dup_val)

        if not can_ph:
            # Canonical has a real value — keep it.
            note = ""
            if not dup_ph and norm_for_search(str(can_val)) != norm_for_search(str(dup_val)):
                note = f"  [duplicate had different value: {dup_val!r}]"
            plan.append({
                "field":     field,
                "action":    "keep",
                "can_value": can_val,
                "dup_value": dup_val,
                "note":      note,
            })
        elif not dup_ph:
            # Canonical is placeholder; duplicate has a real value → fill.
            plan.append({
                "field":     field,
                "action":    "fill",
                "can_value": can_val,
                "dup_value": dup_val,
                "note":      "",
            })
        else:
            plan.append({
                "field":     field,
                "action":    "both_placeholder",
                "can_value": can_val,
                "dup_value": dup_val,
                "note":      "",
            })
    return plan


def build_alias_plan(conn, canonical_id, dup_id, dup_primary_name):
    """Build the alias merge plan.

    Returns (inserts, skipped) where each is a list of dicts.
    """
    # Canonical's existing alias norms.
    can_alias_rows = conn.execute(
        "SELECT alias_id, alias_text, alias_norm, alias_type, is_primary, notes, first_chapter_id "
        "FROM aliases WHERE character_id = ?",
        (canonical_id,),
    ).fetchall()
    can_norms = {r["alias_norm"] for r in can_alias_rows}

    # Duplicate's aliases.
    dup_alias_rows = conn.execute(
        "SELECT alias_id, alias_text, alias_norm, alias_type, is_primary, notes, first_chapter_id "
        "FROM aliases WHERE character_id = ?",
        (dup_id,),
    ).fetchall()

    inserts = []
    skipped = []
    seen_norms = set(can_norms)  # track norms we've already queued for insert

    # Process duplicate's existing aliases first.
    for a in dup_alias_rows:
        norm = a["alias_norm"]
        if norm in seen_norms:
            skipped.append({
                "alias_text": a["alias_text"],
                "alias_norm": norm,
                "alias_type": a["alias_type"],
                "reason":     "already present on canonical",
            })
        else:
            seen_norms.add(norm)
            # Preserve the original alias_type and metadata, but never
            # carry over is_primary=1 — the canonical's own primary is already set.
            inserts.append({
                "alias_text":      a["alias_text"],
                "alias_norm":      norm,
                "alias_type":      a["alias_type"] if a["alias_type"] != "primary" else "epithet",
                "is_primary":      0,
                "notes":           a["notes"],
                "first_chapter_id": a["first_chapter_id"],
                "source":          "dup_alias",
            })

    # Add the duplicate's primary_name as an alias on canonical.
    # Use alias_type='epithet' (the most appropriate available type for a
    # "formerly-primary" label; schema CHECK allows only primary/given_name/
    # title/nickname/disguise/epithet).
    dup_pname_norm = norm_for_search(dup_primary_name)
    if dup_pname_norm in seen_norms:
        skipped.append({
            "alias_text": dup_primary_name,
            "alias_norm": dup_pname_norm,
            "alias_type": "epithet",
            "reason":     "already present on canonical (matches dup primary name)",
        })
    else:
        seen_norms.add(dup_pname_norm)
        inserts.append({
            "alias_text":      dup_primary_name,
            "alias_norm":      dup_pname_norm,
            "alias_type":      "epithet",
            "is_primary":      0,
            "notes":           "former primary_name (added by merge_characters.py)",
            "first_chapter_id": None,
            "source":          "dup_primary_name",
        })

    return inserts, skipped


def build_appearance_plan(conn, canonical_id, dup_id):
    """Build the appearance merge plan.

    Returns (reassign, conflicts) where each is a list of dicts.
    """
    can_chapter_ids = {
        r["chapter_id"]
        for r in conn.execute(
            "SELECT chapter_id FROM appearances WHERE character_id = ?",
            (canonical_id,),
        ).fetchall()
    }

    dup_apps = conn.execute(
        """SELECT ap.appearance_id, ap.chapter_id, ap.name_used,
                  ap.whereabouts, ap.notable_actions, ap.alliances_shown,
                  ap.demeanor,
                  b.series_order, ch.chapter_number, ch.title AS chapter_title
             FROM appearances ap
             JOIN chapters ch ON ch.chapter_id = ap.chapter_id
             JOIN books    b  ON  b.book_id    = ch.book_id
            WHERE ap.character_id = ?
            ORDER BY b.series_order, ch.chapter_number""",
        (dup_id,),
    ).fetchall()

    reassign  = []
    conflicts = []
    for ap in dup_apps:
        if ap["chapter_id"] in can_chapter_ids:
            conflicts.append(ap)
        else:
            reassign.append(ap)

    return reassign, conflicts


def build_relationship_plan(conn, canonical_id, dup_id):
    """Build the relationship merge plan.

    Returns (reassign, self_loops, dup_edges) where each is a list of dicts.
    Each dict includes:
        relationship_id, relationship_type, directed, description,
        first_chapter_id, other_id, other_name, dup_side ('a' or 'b')
    """
    dup_rels = conn.execute(
        """SELECT r.relationship_id,
                  r.relationship_type,
                  r.directed,
                  r.description,
                  r.first_chapter_id,
                  CASE WHEN r.character_a = ? THEN 'a' ELSE 'b' END AS dup_side,
                  CASE WHEN r.character_a = ? THEN r.character_b
                       ELSE r.character_a END AS other_id,
                  c.primary_name AS other_name
             FROM relationships r
             JOIN characters c ON c.character_id =
                  (CASE WHEN r.character_a = ? THEN r.character_b
                        ELSE r.character_a END)
            WHERE r.character_a = ? OR r.character_b = ?""",
        (dup_id, dup_id, dup_id, dup_id, dup_id),
    ).fetchall()

    self_loops = []
    dup_edges  = []
    reassign   = []
    # Track (other_id, rel_type) pairs we've already queued for reassign
    # to avoid producing two rows to the same target with the same type.
    seen_reassign = set()

    for r in dup_rels:
        other_id = r["other_id"]
        rel_type = r["relationship_type"]
        directed = r["directed"]

        # Self-loop: the other party is the canonical.
        if other_id == canonical_id:
            self_loops.append(dict(r))
            continue

        # Dedup within the duplicate's own relationships: if dup somehow has
        # both (dup, X, T) and (X, dup, T) for undirected edges, only the
        # first is reassigned; the second would become a DB-level duplicate.
        dedup_key = (other_id, rel_type)
        if dedup_key in seen_reassign:
            dup_edges.append(dict(r))
            continue

        # Check if canonical already has an equivalent edge to other_id.
        if directed:
            # Directed: preserve side.  canonical takes dup's position.
            if r["dup_side"] == "a":
                dup_query = (
                    "SELECT relationship_id FROM relationships "
                    "WHERE character_a = ? AND character_b = ? "
                    "AND relationship_type = ? AND directed = 1",
                    (canonical_id, other_id, rel_type),
                )
            else:
                dup_query = (
                    "SELECT relationship_id FROM relationships "
                    "WHERE character_a = ? AND character_b = ? "
                    "AND relationship_type = ? AND directed = 1",
                    (other_id, canonical_id, rel_type),
                )
        else:
            # Undirected: check both orderings.
            dup_query = (
                "SELECT relationship_id FROM relationships "
                "WHERE relationship_type = ? AND directed = 0 "
                "AND ((character_a = ? AND character_b = ?) "
                "     OR (character_a = ? AND character_b = ?))",
                (rel_type, canonical_id, other_id, other_id, canonical_id),
            )

        existing = conn.execute(*dup_query).fetchone()
        if existing:
            dup_edges.append(dict(r))
        else:
            seen_reassign.add(dedup_key)
            reassign.append(dict(r))

    return reassign, self_loops, dup_edges


def build_faction_plan(conn, canonical_id, dup_id):
    """Build the character_factions merge plan.

    Returns (reassign, duplicates) where each is a list of dicts.
    """
    can_factions = {
        r["faction_id"]
        for r in conn.execute(
            "SELECT faction_id FROM character_factions WHERE character_id = ?",
            (canonical_id,),
        ).fetchall()
    }

    dup_factions = conn.execute(
        """SELECT cf.faction_id, cf.role, cf.first_chapter_id, cf.notes,
                  f.name AS faction_name, f.faction_type
             FROM character_factions cf
             JOIN factions f ON f.faction_id = cf.faction_id
            WHERE cf.character_id = ?
            ORDER BY f.name""",
        (dup_id,),
    ).fetchall()

    reassign   = []
    duplicates = []
    for cf in dup_factions:
        if cf["faction_id"] in can_factions:
            duplicates.append(dict(cf))
        else:
            reassign.append(dict(cf))

    return reassign, duplicates


# ── Dossier printing ──────────────────────────────────────────────────────────

def print_dossier(canonical, duplicate,
                  trait_plan,
                  alias_inserts, alias_skipped,
                  app_reassign, app_conflicts,
                  rel_reassign, self_loops, dup_edges,
                  fac_reassign, fac_dups):

    _header("IDENTITY")
    print(f"\n  CANONICAL  character_id={canonical['character_id']}  "
          f"\"{canonical['primary_name']}\"  [{canonical['character_type']}]")
    print(f"  DUPLICATE  character_id={duplicate['character_id']}  "
          f"\"{duplicate['primary_name']}\"  [{duplicate['character_type']}]")

    _header("CHARACTER ROW MERGE PLAN")
    for p in trait_plan:
        marker = {
            "keep":           "KEEP     ",
            "fill":           "FILL     ",
            "both_placeholder": "—        ",
        }[p["action"]]
        print(f"\n  {marker}  {p['field']}")
        if p["action"] == "keep":
            print(f"    canonical : {p['can_value']!r}")
            if p["note"]:
                print(f"    {p['note'].strip()}")
        elif p["action"] == "fill":
            print(f"    canonical : {p['can_value']!r}  →  {p['dup_value']!r}  (from duplicate)")
        else:
            print(f"    both NULL / placeholder — unchanged")

    _header(f"ALIAS MERGE PLAN  (insert {len(alias_inserts)}, skip {len(alias_skipped)})")
    if alias_inserts:
        print()
        for a in alias_inserts:
            src = "  [duplicate's primary_name]" if a["source"] == "dup_primary_name" else ""
            print(f"  INSERT  \"{a['alias_text']}\"  [{a['alias_type']}]{src}")
            if a["notes"]:
                print(f"    notes: {a['notes']!r}")
    if alias_skipped:
        print()
        for a in alias_skipped:
            print(f"  SKIP    \"{a['alias_text']}\"  — {a['reason']}")

    _header(f"APPEARANCE MERGE PLAN  "
            f"(reassign {len(app_reassign)}, conflict {len(app_conflicts)})")
    if app_reassign:
        print()
        for ap in app_reassign:
            print(f"  REASSIGN  Bk{ap['series_order']} Ch{ap['chapter_number']:>3}  "
                  f"\"{ap['chapter_title']}\"")
    if app_conflicts:
        print()
        for ap in app_conflicts:
            print(f"  CONFLICT  Bk{ap['series_order']} Ch{ap['chapter_number']:>3}  "
                  f"\"{ap['chapter_title']}\"  — canonical already has this chapter")

    _header(f"RELATIONSHIP MERGE PLAN  "
            f"(reassign {len(rel_reassign)}, self-loop {len(self_loops)}, "
            f"dup-edge {len(dup_edges)})")
    for r in rel_reassign:
        directed_str = "directed" if r["directed"] else "undirected"
        side = "character_a" if r["dup_side"] == "a" else "character_b"
        print(f"\n  REASSIGN  [{r['relationship_type']} / {directed_str}]  "
              f"dup was {side}")
        print(f"    other party: \"{r['other_name']}\"")
        if r["description"]:
            print(f"    description: {r['description']!r}")
    for r in self_loops:
        directed_str = "directed" if r["directed"] else "undirected"
        print(f"\n  SELF-LOOP  [{r['relationship_type']} / {directed_str}]  "
              f"other party is canonical — discard")
    for r in dup_edges:
        directed_str = "directed" if r["directed"] else "undirected"
        print(f"\n  DUP-EDGE   [{r['relationship_type']} / {directed_str}]  "
              f"canonical already has this edge or already queued — discard")
        print(f"    other party: \"{r['other_name']}\"")

    _header(f"FACTION MERGE PLAN  "
            f"(reassign {len(fac_reassign)}, duplicate {len(fac_dups)})")
    if fac_reassign:
        print()
        for cf in fac_reassign:
            print(f"  REASSIGN  \"{cf['faction_name']}\"  [{cf['faction_type']}]  "
                  f"role={cf['role']!r}")
    if fac_dups:
        print()
        for cf in fac_dups:
            print(f"  DUPLICATE  \"{cf['faction_name']}\"  [{cf['faction_type']}]  "
                  f"— canonical already a member")

    _header("SUMMARY")
    print(f"\n  Character row fills      : "
          f"{sum(1 for p in trait_plan if p['action'] == 'fill')}")
    print(f"  Alias inserts            : {len(alias_inserts)}")
    print(f"  Alias skipped            : {len(alias_skipped)}")
    print(f"  Appearance reassigns     : {len(app_reassign)}")
    print(f"  Appearance conflicts     : {len(app_conflicts)}")
    print(f"  Relationship reassigns   : {len(rel_reassign)}")
    print(f"  Relationship self-loops  : {len(self_loops)}")
    print(f"  Relationship dup-edges   : {len(dup_edges)}")
    print(f"  Faction reassigns        : {len(fac_reassign)}")
    print(f"  Faction duplicates       : {len(fac_dups)}")


# ── Commit ────────────────────────────────────────────────────────────────────

def do_merge(conn, canonical, duplicate,
             trait_plan,
             alias_inserts,
             app_reassign, app_conflicts,
             rel_reassign, self_loops, dup_edges,
             fac_reassign, fac_dups):
    """Execute the merge inside the caller's transaction.

    Returns a dict of actual row counts (for the final report).
    Raises on any error — caller rolls back and exits.
    """
    canonical_id = canonical["character_id"]
    dup_id       = duplicate["character_id"]

    # ── a. UPDATE canonical's character row for FILL fields ───────────────────
    fills = [(p["field"], p["dup_value"]) for p in trait_plan if p["action"] == "fill"]
    if fills:
        set_clause = ", ".join(f"{field} = ?" for field, _ in fills)
        values     = [val for _, val in fills] + [canonical_id]
        conn.execute(
            f"UPDATE characters SET {set_clause} WHERE character_id = ?",
            values,
        )

    # ── b. INSERT new aliases on canonical, then DELETE all of duplicate's ──────
    # INSERT the non-duplicate aliases first (plan was computed before any
    # modification, so the rows are exactly what build_alias_plan found).
    n_alias_inserted = 0
    for a in alias_inserts:
        conn.execute(
            """INSERT INTO aliases
                   (character_id, alias_text, alias_norm, alias_type,
                    is_primary, notes, first_chapter_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                canonical_id,
                a["alias_text"],
                a["alias_norm"],
                a["alias_type"],
                a["is_primary"],
                a["notes"],
                a["first_chapter_id"],
            ),
        )
        n_alias_inserted += 1

    # Now DELETE every alias row that still points at the duplicate.
    # This covers both:
    #   • rows that were SKIP-marked (already present on canonical, so no
    #     INSERT was needed) — these were the 8 rows that tripped the orphan
    #     check; they must be deleted even though canonical already has them.
    #   • rows that were INSERT-marked (already moved to canonical above).
    # The INSERT plan was fully applied before this DELETE, so nothing is lost.
    conn.execute(
        "DELETE FROM aliases WHERE character_id = ?",
        (dup_id,),
    )

    # ── c. Appearances ────────────────────────────────────────────────────────
    # Delete conflicting duplicate-side appearance rows first.
    if app_conflicts:
        conflict_app_ids = [ap["appearance_id"] for ap in app_conflicts]
        ph = ",".join("?" * len(conflict_app_ids))
        conn.execute(
            f"DELETE FROM appearances WHERE appearance_id IN ({ph})",
            conflict_app_ids,
        )

    # Reassign non-conflicting appearances to canonical.
    n_app_reassigned = 0
    if app_reassign:
        reassign_app_ids = [ap["appearance_id"] for ap in app_reassign]
        ph = ",".join("?" * len(reassign_app_ids))
        conn.execute(
            f"UPDATE appearances SET character_id = ? "
            f"WHERE appearance_id IN ({ph})",
            [canonical_id] + reassign_app_ids,
        )
        n_app_reassigned = len(reassign_app_ids)

    # ── c2. Mentions ──────────────────────────────────────────────────────────
    # The mentions table postdates this tool. Same rule as appearances:
    # at most one mention per (character_id, chapter_id), and a chapter where
    # the canonical is PRESENT (has an appearance) must not also carry a mention.
    # Drop duplicate-side mentions that would collide, then reassign the rest.
    conn.execute(
        "DELETE FROM mentions WHERE character_id = ? AND chapter_id IN "
        "(SELECT chapter_id FROM mentions   WHERE character_id = ? "
        " UNION SELECT chapter_id FROM appearances WHERE character_id = ?)",
        (dup_id, canonical_id, canonical_id),
    )
    n_men_reassigned = conn.execute(
        "UPDATE mentions SET character_id = ? WHERE character_id = ?",
        (canonical_id, dup_id),
    ).rowcount
    # A reassigned mention may now collide with a canonical appearance moved in
    # this same merge — present beats mentioned, so drop those.
    conn.execute(
        "DELETE FROM mentions WHERE character_id = ? AND chapter_id IN "
        "(SELECT chapter_id FROM appearances WHERE character_id = ?)",
        (canonical_id, canonical_id),
    )

    # ── d. Relationships ──────────────────────────────────────────────────────
    # Collect IDs to delete (self-loops + duplicate edges).
    delete_rel_ids = (
        [r["relationship_id"] for r in self_loops]
        + [r["relationship_id"] for r in dup_edges]
    )
    if delete_rel_ids:
        ph = ",".join("?" * len(delete_rel_ids))
        conn.execute(
            f"DELETE FROM relationships WHERE relationship_id IN ({ph})",
            delete_rel_ids,
        )

    # Reassign remaining relationships.  Must update character_a or character_b
    # individually depending on which side the duplicate occupied, so that the
    # directed semantics (character_a is the edge source) are preserved.
    n_rel_reassigned = 0
    for r in rel_reassign:
        if r["dup_side"] == "a":
            conn.execute(
                "UPDATE relationships SET character_a = ? "
                "WHERE relationship_id = ?",
                (canonical_id, r["relationship_id"]),
            )
        else:
            conn.execute(
                "UPDATE relationships SET character_b = ? "
                "WHERE relationship_id = ?",
                (canonical_id, r["relationship_id"]),
            )
        n_rel_reassigned += 1

    # ── e. character_factions ─────────────────────────────────────────────────
    # Delete duplicate memberships (canonical already has that faction).
    if fac_dups:
        dup_faction_ids = [cf["faction_id"] for cf in fac_dups]
        ph = ",".join("?" * len(dup_faction_ids))
        conn.execute(
            f"DELETE FROM character_factions "
            f"WHERE character_id = ? AND faction_id IN ({ph})",
            [dup_id] + dup_faction_ids,
        )

    # Reassign remaining faction memberships to canonical.
    n_fac_reassigned = 0
    if fac_reassign:
        conn.execute(
            "UPDATE character_factions SET character_id = ? "
            "WHERE character_id = ?",
            (canonical_id, dup_id),
        )
        n_fac_reassigned = len(fac_reassign)

    # ── f. Orphan check ───────────────────────────────────────────────────────
    # Every dependent table must now have zero rows pointing at dup_id.
    # If any remain, we have a bug — rollback before deleting the character row.
    orphan_checks = [
        ("aliases",
         "SELECT COUNT(*) FROM aliases WHERE character_id = ?"),
        ("appearances",
         "SELECT COUNT(*) FROM appearances WHERE character_id = ?"),
        ("mentions",
         "SELECT COUNT(*) FROM mentions WHERE character_id = ?"),
        ("relationships",
         "SELECT COUNT(*) FROM relationships "
         "WHERE character_a = ? OR character_b = ?"),
        ("character_factions",
         "SELECT COUNT(*) FROM character_factions WHERE character_id = ?"),
    ]
    for table, sql in orphan_checks:
        if "character_a" in sql:
            count = conn.execute(sql, (dup_id, dup_id)).fetchone()[0]
        else:
            count = conn.execute(sql, (dup_id,)).fetchone()[0]
        if count > 0:
            raise RuntimeError(
                f"Orphan check FAILED: {table} still has {count} row(s) "
                f"pointing at duplicate character_id={dup_id}.  "
                f"This is a bug — rolling back."
            )

    # ── g. DELETE the duplicate's character row ───────────────────────────────
    conn.execute(
        "DELETE FROM characters WHERE character_id = ?", (dup_id,)
    )

    return {
        "fills":           len(fills),
        "alias_inserted":  n_alias_inserted,
        "app_reassigned":  n_app_reassigned,
        "app_deleted":     len(app_conflicts),
        "men_reassigned":  n_men_reassigned,
        "rel_reassigned":  n_rel_reassigned,
        "rel_deleted":     len(delete_rel_ids),
        "fac_reassigned":  n_fac_reassigned,
        "fac_deleted":     len(fac_dups),
    }


def verify_merge(conn, canonical_id, expected_alias_count,
                 expected_app_count, expected_rel_count, expected_fac_count):
    """Re-query canonical's counts and compare against expectations.

    Returns a list of discrepancy strings (empty = all good).
    """
    errors = []

    got_aliases = conn.execute(
        "SELECT COUNT(*) FROM aliases WHERE character_id = ?", (canonical_id,)
    ).fetchone()[0]
    if got_aliases != expected_alias_count:
        errors.append(
            f"aliases: expected {expected_alias_count}, got {got_aliases}"
        )

    got_apps = conn.execute(
        "SELECT COUNT(*) FROM appearances WHERE character_id = ?", (canonical_id,)
    ).fetchone()[0]
    if got_apps != expected_app_count:
        errors.append(
            f"appearances: expected {expected_app_count}, got {got_apps}"
        )

    got_rels = conn.execute(
        "SELECT COUNT(*) FROM relationships "
        "WHERE character_a = ? OR character_b = ?",
        (canonical_id, canonical_id),
    ).fetchone()[0]
    if got_rels != expected_rel_count:
        errors.append(
            f"relationships: expected {expected_rel_count}, got {got_rels}"
        )

    got_facs = conn.execute(
        "SELECT COUNT(*) FROM character_factions WHERE character_id = ?",
        (canonical_id,),
    ).fetchone()[0]
    if got_facs != expected_fac_count:
        errors.append(
            f"character_factions: expected {expected_fac_count}, got {got_facs}"
        )

    return errors


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=(
            "Fold a duplicate character row into a canonical row, "
            "retargeting all dependent data, then delete the duplicate."
        ),
    )
    ap.add_argument(
        "--target", required=True, metavar="PATH",
        help="Database to operate on (read-write).",
    )
    ap.add_argument(
        "--canonical", required=True, metavar="NAME",
        help="primary_name of the character row to KEEP.",
    )
    ap.add_argument(
        "--duplicate", required=True, metavar="NAME",
        help="primary_name of the row to FOLD IN and then DELETE.",
    )
    ap.add_argument(
        "--commit", action="store_true",
        help=(
            "Apply the merge to --target.  Without --commit the script is a "
            "dry-run: it prints the full merge plan but makes no changes."
        ),
    )
    args = ap.parse_args()

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print()
    print(_SEP2)
    print("  WoT CHARACTER DIRECTORY — MERGE CHARACTERS")
    print(f"  Mode      : {mode}")
    print(f"  target    : {pathlib.Path(args.target).resolve()}")
    print(f"  canonical : {args.canonical!r}")
    print(f"  duplicate : {args.duplicate!r}")
    print(_SEP2)

    # ── Step 1: open database ─────────────────────────────────────────────────
    conn = open_db(args.target)

    # ── Step 2: find both characters ──────────────────────────────────────────
    canonical = find_character(conn, args.canonical, "canonical")
    duplicate = find_character(conn, args.duplicate, "duplicate")

    if canonical["character_id"] == duplicate["character_id"]:
        conn.close()
        sys.exit(
            f"\nERROR: --canonical and --duplicate resolve to the same row "
            f"(character_id={canonical['character_id']}).  Cannot self-merge."
        )

    # character_type mismatch → HALT.
    if canonical["character_type"] != duplicate["character_type"]:
        conn.close()
        sys.exit(
            f"\nERROR: character_type mismatch — "
            f"canonical is {canonical['character_type']!r}, "
            f"duplicate is {duplicate['character_type']!r}.  "
            f"Merging characters of different types is not allowed."
        )

    canonical_id = canonical["character_id"]
    dup_id       = duplicate["character_id"]

    # ── Step 3–7: build all plan objects ─────────────────────────────────────
    trait_plan                      = build_trait_plan(canonical, duplicate)
    alias_inserts, alias_skipped    = build_alias_plan(
        conn, canonical_id, dup_id, duplicate["primary_name"]
    )
    app_reassign, app_conflicts     = build_appearance_plan(conn, canonical_id, dup_id)
    rel_reassign, self_loops, dup_edges = build_relationship_plan(
        conn, canonical_id, dup_id
    )
    fac_reassign, fac_dups          = build_faction_plan(conn, canonical_id, dup_id)

    # ── Step 8: print full dossier ────────────────────────────────────────────
    print_dossier(
        canonical, duplicate,
        trait_plan,
        alias_inserts, alias_skipped,
        app_reassign, app_conflicts,
        rel_reassign, self_loops, dup_edges,
        fac_reassign, fac_dups,
    )

    # ── Dry-run exit ──────────────────────────────────────────────────────────
    if not args.commit:
        print()
        print(_SEP)
        print("  Dry-run complete.  No changes made.")
        print("  Re-run with --commit to apply the merge.")
        conn.close()
        return

    # ── Step 9: commit ────────────────────────────────────────────────────────
    _header("MERGING")
    take_backup(args.target, args.canonical)
    print()

    # Pre-merge counts (for post-merge verification).
    pre_can_aliases = conn.execute(
        "SELECT COUNT(*) FROM aliases WHERE character_id = ?", (canonical_id,)
    ).fetchone()[0]
    pre_can_apps = conn.execute(
        "SELECT COUNT(*) FROM appearances WHERE character_id = ?", (canonical_id,)
    ).fetchone()[0]
    pre_can_rels = conn.execute(
        "SELECT COUNT(*) FROM relationships "
        "WHERE character_a = ? OR character_b = ?",
        (canonical_id, canonical_id),
    ).fetchone()[0]
    pre_can_facs = conn.execute(
        "SELECT COUNT(*) FROM character_factions WHERE character_id = ?",
        (canonical_id,),
    ).fetchone()[0]

    # Expected post-merge counts.
    exp_aliases = pre_can_aliases + len(alias_inserts)
    exp_apps    = pre_can_apps    + len(app_reassign)
    exp_rels    = pre_can_rels    + len(rel_reassign)
    exp_facs    = pre_can_facs    + len(fac_reassign)

    try:
        counts = do_merge(
            conn, canonical, duplicate,
            trait_plan,
            alias_inserts,
            app_reassign, app_conflicts,
            rel_reassign, self_loops, dup_edges,
            fac_reassign, fac_dups,
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        sys.exit(
            f"\nERROR during merge: {exc}\n"
            f"All changes rolled back.  Database is unchanged."
        )

    # ── Verify ────────────────────────────────────────────────────────────────
    verify_errors = verify_merge(
        conn, canonical_id, exp_aliases, exp_apps, exp_rels, exp_facs
    )
    if verify_errors:
        print("  WARNING: post-merge verification found discrepancies:")
        for e in verify_errors:
            print(f"    {e}")
        print("  Investigate before relying on the merged data.")
    else:
        print("  Verified: all counts match expectations.")

    # ── Final report ──────────────────────────────────────────────────────────
    _header("MERGED")
    print(f"\n  Duplicate {duplicate['primary_name']!r}  "
          f"(cid={dup_id}) folded into "
          f"{canonical['primary_name']!r}  (cid={canonical_id})")
    print(f"  character row fills     : {counts['fills']}")
    print(f"  aliases inserted        : {counts['alias_inserted']}")
    print(f"  appearances reassigned  : {counts['app_reassigned']}")
    print(f"  appearances deleted     : {counts['app_deleted']}")
    print(f"  relationships reassigned: {counts['rel_reassigned']}")
    print(f"  relationships deleted   : {counts['rel_deleted']}")
    print(f"  factions reassigned     : {counts['fac_reassigned']}")
    print(f"  factions deleted        : {counts['fac_deleted']}")
    print(f"\n  Canonical now has:")
    print(f"    {exp_aliases} aliases, {exp_apps} appearances, "
          f"{exp_rels} relationships, {exp_facs} faction memberships")

    conn.close()


if __name__ == "__main__":
    main()
