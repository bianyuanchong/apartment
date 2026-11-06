-- Per-building feed freshness (updated by scripts/daily_sync.py unit ingest).

ALTER TABLE apartment.apartment
    ADD COLUMN IF NOT EXISTS last_feed_date DATE,
    ADD COLUMN IF NOT EXISTS last_feed_unit_count INTEGER;

COMMENT ON COLUMN apartment.apartment.last_feed_date IS
    'as_of_date of the most recent daily_feed ingest that included this building';
COMMENT ON COLUMN apartment.apartment.last_feed_unit_count IS
    'Unit rows for this building on last_feed_date';
