-- Migration 004: decouple snapshot slot and display label from series_order
--
-- books.series_order stays the READING-order sort key (New Spring = 11, Knife
-- of Dreams = 12, ...). Two new columns carry what that integer used to be
-- overloaded with:
--   slot  - storage identity; the snapshot file is db/wot_{slot}.db
--           ('book1'..'book10', 'newspring', 'book11' = Knife of Dreams ...)
--   label - short display text for the site ('Book 10', 'New Spring', 'Book 11')
--
-- SQLite cannot ADD COLUMN ... NOT NULL without a default, so the columns are
-- added nullable here, backfilled, and NOT NULL is enforced at write time by
-- parse_epub.py / start_book.py (and in schema.sql for fresh databases).
-- Re-running errors with "duplicate column name", the "already applied" signal.
-- Apply to every snapshot with:  python scripts/apply_migration.py --migration 004 --commit

ALTER TABLE books ADD COLUMN slot TEXT;
ALTER TABLE books ADD COLUMN label TEXT;
UPDATE books SET slot = 'book' || series_order WHERE slot IS NULL;
UPDATE books SET label = 'Book ' || series_order WHERE label IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_books_slot ON books(slot);
