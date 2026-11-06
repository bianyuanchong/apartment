from __future__ import annotations

RECENT_CHANGES_SQL = """
WITH ranked_dates AS (
    SELECT as_of_date, ROW_NUMBER() OVER (ORDER BY as_of_date DESC) AS rn
    FROM (SELECT DISTINCT as_of_date FROM apartment.daily_feed) d
),
bounds AS (
    SELECT
        MAX(CASE WHEN rn = 1 THEN as_of_date END) AS latest_date,
        MAX(CASE WHEN rn = 2 THEN as_of_date END) AS prev_date
    FROM ranked_dates
),
cur AS (
    SELECT df.apartment_id, df.unit_number, df.rent_min, df.rent_max, df.as_of_date
    FROM apartment.daily_feed df
    CROSS JOIN bounds b
    WHERE df.as_of_date = b.latest_date
),
prev AS (
    SELECT df.apartment_id, df.unit_number, df.rent_min, df.rent_max
    FROM apartment.daily_feed df
    CROSS JOIN bounds b
    WHERE df.as_of_date = b.prev_date
)
SELECT
    b.latest_date,
    b.prev_date,
    a.apartment_id,
    a.name AS apartment_name,
    a.zip,
    c.unit_number,
    p.rent_min AS prev_rent_min,
    p.rent_max AS prev_rent_max,
    c.rent_min AS latest_rent_min,
    c.rent_max AS latest_rent_max,
    (c.rent_min - p.rent_min) AS delta_rent_min
FROM bounds b
JOIN cur c ON TRUE
JOIN prev p ON p.apartment_id = c.apartment_id AND p.unit_number = c.unit_number
JOIN apartment.apartment a ON a.apartment_id = c.apartment_id
WHERE c.rent_min IS NOT NULL
  AND p.rent_min IS NOT NULL
  AND c.rent_min IS DISTINCT FROM p.rent_min
ORDER BY delta_rent_min ASC, a.name, c.unit_number
"""

WATCHLIST_SQL = """
SELECT
    pa.alert_id,
    pa.apartment_id,
    pa.unit_number,
    pa.target_rent,
    pa.direction,
    pa.note,
    pa.created_at,
    a.name AS apartment_name,
    a.zip,
    lat.rent_min AS latest_rent_min,
    lat.rent_max AS latest_rent_max,
    lat.as_of_date AS latest_as_of_date
FROM apartment.price_alert pa
JOIN apartment.apartment a ON a.apartment_id = pa.apartment_id
LEFT JOIN LATERAL (
    SELECT df.rent_min, df.rent_max, df.as_of_date
    FROM apartment.daily_feed df
    WHERE df.apartment_id = pa.apartment_id
      AND df.unit_number = pa.unit_number
    ORDER BY df.as_of_date DESC
    LIMIT 1
) lat ON TRUE
WHERE pa.is_active
ORDER BY pa.created_at DESC
"""

INSERT_ALERT_SQL = """
INSERT INTO apartment.price_alert (
    apartment_id, unit_number, target_rent, direction, note
) VALUES (%s, %s, %s, %s, %s)
RETURNING alert_id, apartment_id, unit_number, target_rent, direction, note, created_at
"""

DELETE_ALERT_SQL = """
UPDATE apartment.price_alert SET is_active = FALSE WHERE alert_id = %s AND is_active
RETURNING alert_id
"""


def alert_triggered(direction: str, target_rent, latest_rent_min) -> bool:
    if latest_rent_min is None or target_rent is None:
        return False
    target = float(target_rent)
    latest = float(latest_rent_min)
    if direction == "below":
        return latest <= target
    return latest >= target
