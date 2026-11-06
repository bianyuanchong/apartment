from __future__ import annotations

import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from merged_group_by_building import transform  # noqa: E402

from modules.aws_utils import load_aws_config, s3_client, s3_get_json
from modules.apartment_feed_tracking import sync_apartment_feed_metadata
from modules.building_apartment_map import load_slug_to_apartment_id
from modules.database_utils import db_connect

INSERT_SQL = """
INSERT INTO apartment.daily_feed (
    apartment_id, unit_number, as_of_date, floorplan, bedrooms, bathrooms,
    size_sqft, rent_min, rent_max, availability, move_in_date,
    months_free, op, discount
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
"""


def parse_as_of_date(merged: dict[str, Any], source_hint: str | None = None) -> date:
    raw = merged.get("scraped_date") or source_hint
    if raw:
        return date.fromisoformat(str(raw)[:10])
    match = re.search(r"(\d{4}-\d{2}-\d{2})", source_hint or "")
    if match:
        return date.fromisoformat(match.group(1))
    raise ValueError("Could not determine as_of_date")


def parse_move_in_date(available: Any, as_of: date) -> date | None:
    if available is None:
        return None
    text = str(available).strip()
    if not text or text.lower() == "now":
        return as_of
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_size_sqft(value: Any) -> int | None:
    if value is None:
        return None
    try:
        sqft = int(value)
    except (TypeError, ValueError):
        return None
    if sqft <= 1:
        return None
    return sqft


def load_building_id_map(cur) -> dict[str, int]:
    """Slug → apartment_id via building_catalog names + pending: URLs (see building_apartment_map)."""
    return load_slug_to_apartment_id(cur)


def rows_from_merged(
    merged: dict[str, Any],
    building_id_map: dict[str, int],
    as_of: date | None = None,
) -> tuple[list[tuple], dict[str, int]]:
    as_of_date = as_of or parse_as_of_date(merged)
    by_building = transform(merged)["buildings"]

    rows: list[tuple] = []
    stats = {"buildings": 0, "units": 0, "skipped_buildings": 0, "skipped_units": 0}

    for building in by_building:
        stats["buildings"] += 1
        slug = building["id"]
        apartment_id = building_id_map.get(slug)
        if apartment_id is None:
            stats["skipped_buildings"] += 1
            stats["skipped_units"] += len(building.get("units") or [])
            continue

        for unit in building.get("units") or []:
            unit_number = str(unit.get("unit") or "").strip()
            if not unit_number:
                stats["skipped_units"] += 1
                continue

            available = unit.get("available")
            rows.append(
                (
                    apartment_id,
                    unit_number,
                    as_of_date,
                    unit.get("floorplan"),
                    unit.get("beds"),
                    unit.get("baths"),
                    normalize_size_sqft(unit.get("sqft")),
                    unit.get("price_min"),
                    unit.get("price_max"),
                    str(available) if available is not None else None,
                    parse_move_in_date(available, as_of_date),
                    unit.get("months_free"),
                    unit.get("op"),
                    unit.get("discount"),
                )
            )
            stats["units"] += 1

    return rows, stats


def ingest_merged_payload(
    merged: dict[str, Any],
    *,
    source_label: str = "",
    as_of: date | None = None,
) -> dict[str, Any]:
    as_of_date = as_of or parse_as_of_date(merged, source_label)
    conn, cur = db_connect()
    try:
        building_id_map = load_building_id_map(cur)
        rows, stats = rows_from_merged(merged, building_id_map, as_of_date)

        cur.execute("DELETE FROM apartment.daily_feed WHERE as_of_date = %s", (as_of_date,))
        deleted = cur.rowcount

        if rows:
            cur.executemany(INSERT_SQL, rows)

        feed_meta = sync_apartment_feed_metadata(cur, as_of_date, rows)

        conn.commit()
        return {
            "source": source_label,
            "as_of_date": as_of_date.isoformat(),
            "deleted_rows": deleted,
            "inserted_rows": len(rows),
            **stats,
            **feed_meta,
        }
    finally:
        conn.close()


def list_s3_merged_keys() -> list[str]:
    cfg = load_aws_config()
    prefix = cfg["s3_prefix"].rstrip("/")
    client = s3_client()
    keys: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=cfg["s3_bucket"], Prefix=f"{prefix}/"
    ):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            name = key.rsplit("/", 1)[-1]
            if name.startswith("merged_") and name.endswith(".json"):
                keys.append(key)
    return sorted(keys)


def ingest_merged_from_s3(key: str) -> dict[str, Any]:
    merged = s3_get_json(key)
    return ingest_merged_payload(merged, source_label=key)


def ingest_all_s3_merged() -> list[dict[str, Any]]:
    return [ingest_merged_from_s3(key) for key in list_s3_merged_keys()]
