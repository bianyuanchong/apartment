from __future__ import annotations

import re
from datetime import date
from typing import Any

from modules.aws_utils import load_aws_config, s3_client, s3_get_json, s3_key, s3_object_exists
from modules.database_utils import db_connect
from modules.apartment_feed_tracking import sync_apartment_feed_metadata
from modules.building_apartment_map import load_apartment_names
from modules.daily_feed_ingest import INSERT_SQL, normalize_size_sqft, parse_move_in_date

LEGACY_UNIT_KEYS = frozenset(
    {"property_id", "property", "building", "parent_property_id", "parent_property_name"}
)
NAME_HINT_KEYS = ("apartment_name", "building_name", "name")

UNIT_FEED_NAME = "unit_daily_feed.json"
DATE_FOLDER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})/" + re.escape(UNIT_FEED_NAME) + "$")


def _parse_payload(payload: Any) -> tuple[date, list[dict[str, Any]]]:
    if isinstance(payload, dict):
        raw_date = payload.get("as_of_date")
        if not raw_date:
            raise ValueError("unit_daily_feed object missing as_of_date")
        as_of = date.fromisoformat(str(raw_date)[:10])
        units = payload.get("units")
        if not isinstance(units, list):
            raise ValueError("unit_daily_feed object missing units array")
        return as_of, units

    if isinstance(payload, list):
        if not payload:
            raise ValueError("Empty unit_daily_feed array")
        dates = {str(row.get("as_of_date"))[:10] for row in payload if row.get("as_of_date")}
        if len(dates) != 1:
            raise ValueError("Array feed must share one as_of_date")
        as_of = date.fromisoformat(next(iter(dates)))
        return as_of, payload

    raise ValueError("Expected unit_daily_feed object or array")


def _folder_date_from_key(key: str, prefix: str) -> date | None:
    rel = key.removeprefix(prefix).lstrip("/")
    match = DATE_FOLDER_RE.match(rel)
    if not match:
        return None
    return date.fromisoformat(match.group(1))


def unit_daily_feed_s3_key(for_date: date) -> str:
    return s3_key(for_date.isoformat(), UNIT_FEED_NAME)


def list_unit_daily_feed_dates() -> list[date]:
    cfg = load_aws_config()
    prefix = cfg["s3_prefix"].rstrip("/") + "/"
    client = s3_client()
    dates: set[date] = set()
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=cfg["s3_bucket"], Prefix=prefix
    ):
        for obj in page.get("Contents") or []:
            folder = _folder_date_from_key(obj["Key"], prefix)
            if folder:
                dates.add(folder)
    return sorted(dates)


def resolve_unit_daily_feed_key(prefer_date: date | None = None) -> str | None:
    if prefer_date is not None:
        key = unit_daily_feed_s3_key(prefer_date)
        return key if s3_object_exists(key) else None

    dates = list_unit_daily_feed_dates()
    if not dates:
        return None
    return unit_daily_feed_s3_key(dates[-1])


def load_valid_apartment_ids(cur) -> set[int]:
    cur.execute("SELECT apartment_id FROM apartment.apartment")
    return {int(row[0]) for row in cur.fetchall()}


def rows_from_units(
    units: list[dict[str, Any]],
    as_of_date: date,
    valid_apartment_ids: set[int],
    *,
    apartment_names: dict[int, str] | None = None,
) -> tuple[list[tuple], dict[str, int]]:
    rows: list[tuple] = []
    stats = {
        "units_in_file": len(units),
        "inserted_units": 0,
        "skipped_unknown_apartment": 0,
        "skipped_missing_unit_number": 0,
        "skipped_duplicate": 0,
        "skipped_legacy_shape": 0,
        "skipped_name_mismatch": 0,
    }
    by_key: dict[tuple[int, str], tuple] = {}

    for unit in units:
        if LEGACY_UNIT_KEYS.intersection(unit.keys()):
            stats["skipped_legacy_shape"] += 1
            continue

        try:
            apartment_id = int(unit["apartment_id"])
        except (KeyError, TypeError, ValueError):
            stats["skipped_unknown_apartment"] += 1
            continue

        if apartment_names is not None:
            name_ok = True
            for hint_key in NAME_HINT_KEYS:
                hint = unit.get(hint_key)
                if hint is None or str(hint).strip() == "":
                    continue
                expected = apartment_names.get(apartment_id)
                if expected and str(hint).strip().lower() != expected.lower():
                    stats["skipped_name_mismatch"] += 1
                    name_ok = False
                    break
            if not name_ok:
                continue

        unit_number = str(unit.get("unit_number") or "").strip()
        if not unit_number:
            stats["skipped_missing_unit_number"] += 1
            continue

        if apartment_id not in valid_apartment_ids:
            stats["skipped_unknown_apartment"] += 1
            continue

        availability = unit.get("availability")
        move_in = unit.get("move_in_date")
        if move_in:
            try:
                move_in_date = date.fromisoformat(str(move_in)[:10])
            except ValueError:
                move_in_date = parse_move_in_date(availability, as_of_date)
        else:
            move_in_date = parse_move_in_date(availability, as_of_date)

        by_key[(apartment_id, unit_number)] = (
            apartment_id,
            unit_number,
            as_of_date,
            unit.get("floorplan"),
            unit.get("bedrooms"),
            unit.get("bathrooms"),
            normalize_size_sqft(unit.get("size_sqft")),
            unit.get("rent_min"),
            unit.get("rent_max"),
            str(availability) if availability is not None else None,
            move_in_date,
            unit.get("months_free"),
            unit.get("op"),
            unit.get("discount"),
        )

    rows = list(by_key.values())
    stats["skipped_duplicate"] = stats["units_in_file"] - stats["skipped_unknown_apartment"] - stats[
        "skipped_missing_unit_number"
    ] - len(rows)
    if stats["skipped_duplicate"] < 0:
        stats["skipped_duplicate"] = 0
    stats["inserted_units"] = len(rows)
    return rows, stats


def ingest_unit_daily_payload(
    payload: Any,
    *,
    source_label: str,
    folder_date: date | None = None,
) -> dict[str, Any]:
    as_of_date, units = _parse_payload(payload)
    if folder_date is not None and folder_date != as_of_date:
        raise ValueError(
            f"Folder date {folder_date} != root as_of_date {as_of_date} in {source_label}"
        )

    conn, cur = db_connect()
    try:
        valid_ids = load_valid_apartment_ids(cur)
        names = load_apartment_names(cur)
        rows, stats = rows_from_units(units, as_of_date, valid_ids, apartment_names=names)

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


def ingest_unit_daily_from_s3(key: str) -> dict[str, Any]:
    cfg = load_aws_config()
    prefix = cfg["s3_prefix"].rstrip("/") + "/"
    folder_date = _folder_date_from_key(key, prefix)
    payload = s3_get_json(key)
    return ingest_unit_daily_payload(payload, source_label=key, folder_date=folder_date)


def ingest_latest_unit_daily_feed(prefer_date: date | None = None) -> dict[str, Any] | None:
    key = resolve_unit_daily_feed_key(prefer_date)
    if not key:
        return None
    return ingest_unit_daily_from_s3(key)
