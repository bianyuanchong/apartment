from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any


def ensure_feed_tracking_columns(cur) -> None:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'apartment'
          AND table_name = 'apartment'
          AND column_name = 'last_feed_date'
        """
    )
    if cur.fetchone():
        return
    cur.execute(
        """
        ALTER TABLE apartment.apartment
            ADD COLUMN IF NOT EXISTS last_feed_date DATE,
            ADD COLUMN IF NOT EXISTS last_feed_unit_count INTEGER
        """
    )


def sync_apartment_feed_metadata(
    cur,
    as_of_date: date,
    feed_rows: list[tuple],
) -> dict[str, Any]:
    """
    After daily_feed insert: set last_feed_* for buildings present in feed_rows.
    Return active buildings that did not appear in this ingest (debug stale/missing).
    """
    ensure_feed_tracking_columns(cur)
    counts = Counter(row[0] for row in feed_rows)

    for apartment_id, unit_count in counts.items():
        cur.execute(
            """
            UPDATE apartment.apartment
            SET last_feed_date = %s,
                last_feed_unit_count = %s
            WHERE apartment_id = %s
            """,
            (as_of_date, unit_count, apartment_id),
        )

    cur.execute(
        """
        SELECT apartment_id, name, last_feed_date, last_feed_unit_count
        FROM apartment.apartment
        WHERE is_active = TRUE
        ORDER BY apartment_id
        """
    )
    missing_today: list[dict[str, Any]] = []
    for apartment_id, name, last_feed_date, last_feed_unit_count in cur.fetchall():
        if apartment_id in counts:
            continue
        missing_today.append(
            {
                "apartment_id": apartment_id,
                "name": name,
                "last_feed_date": last_feed_date.isoformat() if last_feed_date else None,
                "last_feed_unit_count": last_feed_unit_count,
            }
        )

    return {
        "apartments_in_feed": len(counts),
        "active_missing_today": missing_today,
        "active_missing_count": len(missing_today),
    }
