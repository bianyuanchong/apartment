from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from modules.aws_utils import s3_get_json, s3_key, s3_object_exists, s3_put_json
from modules.database_utils import db_connect

VERIFIED_FILENAME = "apartment_list_muse_verified.json"
OUTPUT_FILENAME = "apartment_list.json"

APARTMENT_FIELDS = ("apartment_id", "name", "url", "is_active")


@dataclass
class ApplyStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0


def _normalize_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("apartments"), list):
        rows = payload["apartments"]
    else:
        raise ValueError("Expected a JSON array or {\"apartments\": [...]}")

    out: list[dict[str, Any]] = []
    for row in rows:
        missing = [f for f in APARTMENT_FIELDS if f not in row]
        if missing:
            raise ValueError(f"Row missing fields {missing}: {row!r}")
        out.append(
            {
                "apartment_id": int(row["apartment_id"]),
                "name": str(row["name"]).strip(),
                "url": str(row["url"]).strip(),
                "is_active": bool(row["is_active"]),
            }
        )
    return out


def wrap_apartment_list(rows: list[dict[str, Any]], source: str) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "apartments": rows,
    }


def fetch_apartments_from_db(cur) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT apartment_id, name, url, is_active
        FROM apartment.apartment
        ORDER BY apartment_id
        """
    )
    rows = []
    for apartment_id, name, url, is_active in cur.fetchall():
        rows.append(
            {
                "apartment_id": apartment_id,
                "name": name,
                "url": url,
                "is_active": is_active,
            }
        )
    return rows


def apply_verified_rows(rows: list[dict[str, Any]], cur) -> ApplyStats:
    stats = ApplyStats()
    for row in rows:
        cur.execute(
            """
            SELECT name, url, is_active
            FROM apartment.apartment
            WHERE apartment_id = %s
            """,
            (row["apartment_id"],),
        )
        existing = cur.fetchone()
        if existing is None:
            cur.execute(
                """
                INSERT INTO apartment.apartment (
                    apartment_id, name, url, is_active
                ) VALUES (%s, %s, %s, %s)
                """,
                (row["apartment_id"], row["name"], row["url"], row["is_active"]),
            )
            stats.inserted += 1
            continue

        old_name, old_url, old_active = existing
        if (
            old_name == row["name"]
            and old_url == row["url"]
            and bool(old_active) == row["is_active"]
        ):
            stats.unchanged += 1
            continue

        cur.execute(
            """
            UPDATE apartment.apartment
            SET name = %s, url = %s, is_active = %s, updated_at = NOW()
            WHERE apartment_id = %s
            """,
            (row["name"], row["url"], row["is_active"], row["apartment_id"]),
        )
        stats.updated += 1
    return stats


def sync_from_muse_verified_if_present() -> tuple[bool, ApplyStats | None]:
    key = s3_key(VERIFIED_FILENAME)
    if not s3_object_exists(key):
        return False, None

    payload = s3_get_json(key)
    rows = _normalize_rows(payload)
    conn, cur = db_connect()
    try:
        stats = apply_verified_rows(rows, cur)
        conn.commit()
        return True, stats
    finally:
        conn.close()


def publish_apartment_list_for_muse() -> str:
    key = s3_key(OUTPUT_FILENAME)
    conn, cur = db_connect()
    try:
        rows = fetch_apartments_from_db(cur)
    finally:
        conn.close()

    document = wrap_apartment_list(rows, source="apartment.apartment")
    s3_put_json(key, document)
    return key


def run_apartment_list_pipeline() -> dict[str, Any]:
    """
    Daily apartment catalog step (unit ingest is separate).

    1. If apartment_list_muse_verified.json exists on S3, apply to DB.
    2. Upload apartment_list.json from current DB for Muse's next run.
    """
    result: dict[str, Any] = {
        "verified_key": s3_key(VERIFIED_FILENAME),
        "output_key": s3_key(OUTPUT_FILENAME),
        "verified_applied": False,
    }

    applied, stats = sync_from_muse_verified_if_present()
    result["verified_applied"] = applied
    if stats:
        result["apply_stats"] = stats.__dict__

    output_key = publish_apartment_list_for_muse()
    result["output_key"] = output_key

    conn, cur = db_connect()
    try:
        result["apartment_count"] = len(fetch_apartments_from_db(cur))
    finally:
        conn.close()

    return result
