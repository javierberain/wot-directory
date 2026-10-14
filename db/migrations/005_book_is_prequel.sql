-- Migration 005: books.is_prequel
--
-- New Spring is read after book 10 (series_order 11, spoiler barrier intact) but
-- its story is set twenty years before book 1. The flag tells reconcile.py not to
-- fill time-dependent stable traits (age, physical_traits, personality,
-- filiations) or add faction memberships on characters that ALREADY exist, and
-- lets the site offer a chronological sort of a character's thread.
-- Re-running errors with "duplicate column name", the "already applied" signal.
--     python scripts/apply_migration.py --migration 005 --commit

ALTER TABLE books ADD COLUMN is_prequel INTEGER NOT NULL DEFAULT 0;
UPDATE books SET is_prequel = 1 WHERE slot = 'newspring';
