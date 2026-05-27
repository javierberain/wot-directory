-- ============================================================
-- Migration 001 - species, personality, demeanor, factions
-- ============================================================
-- Applies to an existing wot.db. Non-destructive: adds columns and
-- two new tables in place, never drops or recreates existing data.
--
-- Run once:
--     sqlite3 db/wot.db < db/migrations/001_species_personality_factions.sql
--
-- SQLite has no `ADD COLUMN IF NOT EXISTS`; re-running this file will
-- error on the ALTER statements. That is the intended signal that the
-- migration has already been applied.
-- ============================================================

PRAGMA foreign_keys = ON;

BEGIN TRANSACTION;

-- ----- Change 1: character species ---------------------------
-- Was being stuffed into the free-text `nationality` column.
-- Default 'human' keeps every existing row valid.
ALTER TABLE characters
    ADD COLUMN character_type TEXT NOT NULL DEFAULT 'human';

-- ----- Change 2a: stable personality on the character --------
ALTER TABLE characters
    ADD COLUMN personality TEXT;

-- ----- Change 2b: per-chapter demeanor on the appearance -----
-- How the character presents/behaves in THIS chapter.
ALTER TABLE appearances
    ADD COLUMN demeanor TEXT;

-- ----- Change 3a: factions ------------------------------------
-- Ajahs, military orders, noble houses, clans, societies.
-- name_norm is the lower-cased lookup key, like aliases.alias_norm.
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

-- ----- Change 3b: character_factions join --------------------
-- One row per (character, faction). `role` records whether the
-- character is a plain member, a leader, etc. Notes accrue
-- free-text colour ("former Aiel of the Taardad", etc.).
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

-- Change 4 (Warder bonds) needs no schema change: warder_bond is a new
-- value of relationships.relationship_type. The existing relationships
-- table already supports directed edges and many edges per character.

COMMIT;
