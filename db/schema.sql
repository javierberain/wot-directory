-- ============================================================
-- Wheel of Time Character Directory - SQLite schema
-- ============================================================
-- Design notes:
--   characters         : one row per real entity (stable traits)
--   aliases            : every name a character is known by, typed
--   books              : one row per book
--   chapters           : one row per chapter, holds the raw text
--   appearances        : one row per (character, chapter)
--   relationships      : character-to-character edges, for the graph
--   factions           : Ajahs / orders / houses / clans / societies
--   character_factions : join table, who belongs to which faction
--   review_queue       : extraction items that need a human glance
-- ============================================================

PRAGMA foreign_keys = ON;

-- ----- Books -------------------------------------------------
CREATE TABLE IF NOT EXISTS books (
    book_id      INTEGER PRIMARY KEY,
    series_order INTEGER NOT NULL UNIQUE,   -- 1 = Eye of the World
    title        TEXT    NOT NULL,
    source_file  TEXT                       -- original epub filename
);

-- ----- Chapters ----------------------------------------------
CREATE TABLE IF NOT EXISTS chapters (
    chapter_id     INTEGER PRIMARY KEY,
    book_id        INTEGER NOT NULL REFERENCES books(book_id),
    chapter_number INTEGER NOT NULL,         -- 0 = prologue
    title          TEXT    NOT NULL,
    full_text      TEXT    NOT NULL,
    word_count     INTEGER,
    extracted      INTEGER NOT NULL DEFAULT 0,  -- 0 = not yet run through LLM
    UNIQUE (book_id, chapter_number)
);

-- ----- Characters --------------------------------------------
-- Stable traits only. Anything that varies per chapter lives in
-- appearances. primary_name is the canonical display label.
-- character_type distinguishes species (human/ogier/trolloc/...)
-- from the in-world nationality of a human character. A row whose
-- character_type is 'creature_collective' stands in for a group
-- (e.g. "Trollocs"), not a single individual.
CREATE TABLE IF NOT EXISTS characters (
    character_id     INTEGER PRIMARY KEY,
    primary_name     TEXT NOT NULL UNIQUE,
    character_type   TEXT NOT NULL DEFAULT 'human'
                       CHECK (character_type IN
                       ('human','ogier','trolloc','myrddraal',
                        'horse','wolf','other')),
    nationality      TEXT,
    physical_traits  TEXT,
    age              TEXT,           -- free text; WoT ages are fuzzy
    filiations       TEXT,           -- family / house / parentage
    associations     TEXT,           -- legacy free-text; new code uses factions
    personality      TEXT,           -- stable dispositional traits
    description      TEXT,           -- short summary blurb
    first_chapter_id INTEGER REFERENCES chapters(chapter_id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ----- Aliases -----------------------------------------------
-- Every label a character is known by, including the primary name.
-- alias_type distinguishes a plain spelling from a deliberate mask.
CREATE TABLE IF NOT EXISTS aliases (
    alias_id         INTEGER PRIMARY KEY,
    character_id     INTEGER NOT NULL REFERENCES characters(character_id),
    alias_text       TEXT NOT NULL,
    alias_norm       TEXT NOT NULL,          -- lowercased, for matching
    alias_type       TEXT NOT NULL DEFAULT 'nickname'
                       CHECK (alias_type IN
                       ('primary','given_name','title','nickname',
                        'disguise','epithet')),
    is_primary       INTEGER NOT NULL DEFAULT 0,
    notes            TEXT,                   -- e.g. "used to hide from the Tower"
    first_chapter_id INTEGER REFERENCES chapters(chapter_id),
    UNIQUE (character_id, alias_norm)
);
CREATE INDEX IF NOT EXISTS idx_alias_norm ON aliases(alias_norm);

-- ----- Appearances -------------------------------------------
-- The heart of the chapter-by-chapter model. One row per character
-- per chapter they appear in.
CREATE TABLE IF NOT EXISTS appearances (
    appearance_id   INTEGER PRIMARY KEY,
    character_id    INTEGER NOT NULL REFERENCES characters(character_id),
    chapter_id      INTEGER NOT NULL REFERENCES chapters(chapter_id),
    name_used       TEXT,            -- which name the text used here
    whereabouts     TEXT,            -- where the character is this chapter
    notable_actions TEXT,            -- what they did this chapter
    alliances_shown TEXT,            -- alliances visible this chapter
    demeanor        TEXT,            -- how they present/behave this chapter
    UNIQUE (character_id, chapter_id)
);
CREATE INDEX IF NOT EXISTS idx_app_chapter ON appearances(chapter_id);
CREATE INDEX IF NOT EXISTS idx_app_char    ON appearances(character_id);

-- ----- Relationships -----------------------------------------
-- Character-to-character edges. For the network graph.
-- For undirected types (ally, family) store one row; the app treats
-- directed=0 as symmetric.
CREATE TABLE IF NOT EXISTS relationships (
    relationship_id    INTEGER PRIMARY KEY,
    character_a        INTEGER NOT NULL REFERENCES characters(character_id),
    character_b        INTEGER NOT NULL REFERENCES characters(character_id),
    relationship_type  TEXT NOT NULL,        -- ally, enemy, family, mentor...
    directed           INTEGER NOT NULL DEFAULT 0,
    description        TEXT,
    first_chapter_id   INTEGER REFERENCES chapters(chapter_id),
    UNIQUE (character_a, character_b, relationship_type)
);
CREATE INDEX IF NOT EXISTS idx_rel_a ON relationships(character_a);
CREATE INDEX IF NOT EXISTS idx_rel_b ON relationships(character_b);

-- ----- Factions ----------------------------------------------
-- Ajahs, military orders, noble houses, clans, societies. The
-- generic 'other' bucket catches things that don't fit cleanly.
-- name_norm mirrors aliases.alias_norm: lower-cased for matching.
CREATE TABLE IF NOT EXISTS factions (
    faction_id   INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    name_norm    TEXT NOT NULL UNIQUE,
    faction_type TEXT NOT NULL DEFAULT 'other'
                   CHECK (faction_type IN
                   ('ajah','order','house','clan','society','other')),
    description  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_faction_norm ON factions(name_norm);

-- ----- Character ↔ Faction join ------------------------------
-- One row per (character, faction). `role` records whether the
-- character is a plain member, a leader, etc. The Warder bond is
-- NOT modelled here — it is a character-to-character link and
-- lives in relationships with type 'warder_bond'.
CREATE TABLE IF NOT EXISTS character_factions (
    character_id     INTEGER NOT NULL REFERENCES characters(character_id),
    faction_id       INTEGER NOT NULL REFERENCES factions(faction_id),
    role             TEXT NOT NULL DEFAULT 'member',
    first_chapter_id INTEGER REFERENCES chapters(chapter_id),
    notes            TEXT,
    PRIMARY KEY (character_id, faction_id)
);
CREATE INDEX IF NOT EXISTS idx_cf_character ON character_factions(character_id);
CREATE INDEX IF NOT EXISTS idx_cf_faction   ON character_factions(faction_id);

-- ----- Review queue ------------------------------------------
-- When the extractor or reconciler is unsure, it parks the item here
-- instead of guessing. You clear these by hand.
CREATE TABLE IF NOT EXISTS review_queue (
    review_id   INTEGER PRIMARY KEY,
    chapter_id  INTEGER REFERENCES chapters(chapter_id),
    kind        TEXT NOT NULL,        -- 'ambiguous_character', 'new_character', ...
    payload     TEXT NOT NULL,        -- raw JSON of the item in question
    note        TEXT,
    resolved    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
