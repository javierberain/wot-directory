-- Migration 002: mentions table
-- Adds a place to record characters REFERENCED in a chapter but not present
-- (discussed-in-absentia), kept distinct from appearances. See schema.sql for
-- the rationale. Run once per existing database:
--     sqlite3 db/wot.db < db/migrations/002_mentions.sql
-- Re-running errors on the CREATE (the intended "already applied" signal).

CREATE TABLE mentions (
    mention_id   INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL REFERENCES characters(character_id),
    chapter_id   INTEGER NOT NULL REFERENCES chapters(chapter_id),
    name_used    TEXT,
    context      TEXT,
    UNIQUE (character_id, chapter_id)
);
CREATE INDEX idx_mention_chapter ON mentions(chapter_id);
CREATE INDEX idx_mention_char    ON mentions(character_id);
