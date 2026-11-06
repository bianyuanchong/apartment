-- apartment.apartment_unit: one row per (apartment_id, unit_number) ever seen;
-- latest market fields + stable ref_* for bed/bath checks. History stays in daily_feed.

CREATE TABLE apartment.apartment_unit (
    apartment_id          BIGINT NOT NULL
        REFERENCES apartment.apartment (apartment_id),
    unit_number           VARCHAR(32) NOT NULL,

    first_seen_date       DATE NOT NULL,
    latest_seen_date      DATE NOT NULL,

    ref_bedrooms          NUMERIC(3, 1),
    ref_bathrooms         NUMERIC(3, 1),
    ref_size_sqft         INTEGER,
    ref_floorplan         VARCHAR(255),

    latest_rent_min       NUMERIC(10, 2),
    latest_rent_max       NUMERIC(10, 2),
    latest_availability   VARCHAR(64),
    latest_move_in_date   DATE,

    is_on_market          BOOLEAN NOT NULL DEFAULT FALSE,

    window_direction_1    VARCHAR(16),
    window_direction_2    VARCHAR(16),
    window_direction_3    VARCHAR(16),
    window_direction_4    VARCHAR(16),

    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT apartment_unit_pkey
        PRIMARY KEY (apartment_id, unit_number)
);

CREATE INDEX apartment_unit_latest_seen_idx
    ON apartment.apartment_unit (latest_seen_date DESC);

CREATE INDEX apartment_unit_on_market_idx
    ON apartment.apartment_unit (apartment_id)
    WHERE is_on_market = TRUE;

-- Keep updated_at in sync on any UPDATE (ingest + admin edits).
CREATE OR REPLACE FUNCTION apartment.set_apartment_unit_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

CREATE TRIGGER apartment_unit_updated_at_trg
    BEFORE UPDATE ON apartment.apartment_unit
    FOR EACH ROW
    EXECUTE PROCEDURE apartment.set_apartment_unit_updated_at();
