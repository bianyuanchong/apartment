-- User watchlist: notify when rent crosses target (compared to latest daily_feed snapshot).

CREATE TABLE IF NOT EXISTS apartment.price_alert (
    alert_id BIGSERIAL PRIMARY KEY,
    apartment_id BIGINT NOT NULL REFERENCES apartment.apartment (apartment_id) ON DELETE CASCADE,
    unit_number VARCHAR(32) NOT NULL,
    target_rent NUMERIC(12, 2) NOT NULL,
    direction VARCHAR(8) NOT NULL DEFAULT 'below'
        CHECK (direction IN ('below', 'above')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_price_alert_lookup
    ON apartment.price_alert (apartment_id, unit_number)
    WHERE is_active;
