"""
Fill apartment.latitude / apartment.longitude from address via Nominatim (OSM).

Usage (repo root):
  python scripts/geocode_apartments.py
  python scripts/geocode_apartments.py --force   # re-geocode even if lat/lng set
  python scripts/geocode_apartments.py --dry-run

Nominatim: max 1 request/sec; set NOMINATIM_USER_AGENT if you prefer.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from modules.database_utils import db_connect  # noqa: E402
from modules.geocode_nominatim import DEFAULT_UA, geocode_address, geocode_query  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Re-geocode rows that already have coords")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    user_agent = __import__("os").environ.get("NOMINATIM_USER_AGENT", DEFAULT_UA)

    conn, cur = db_connect()
    cur.execute(
        """
        SELECT apartment_id, name, address, zip, latitude, longitude
        FROM apartment.apartment
        WHERE address IS NOT NULL AND TRIM(address) <> ''
        ORDER BY apartment_id
        """
    )
    rows = cur.fetchall()

    updated = 0
    failed: list[tuple[int, str]] = []
    for apartment_id, name, address, zip_code, lat, lng in rows:
        if lat is not None and lng is not None and not args.force:
            continue
        query = geocode_query(address, zip_code, name=name)
        try:
            coords = geocode_address(query, user_agent)
        except Exception as exc:  # noqa: BLE001
            failed.append((apartment_id, f"{name}: {exc}"))
            time.sleep(1.1)
            continue
        if coords is None:
            failed.append((apartment_id, f"{name}: no result for {query!r}"))
            time.sleep(1.1)
            continue
        glat, glng = coords
        print(f"{apartment_id}\t{name}\t{glat:.6f},{glng:.6f}\t{query}")
        if not args.dry_run:
            cur.execute(
                """
                UPDATE apartment.apartment
                SET latitude = %s, longitude = %s, updated_at = NOW()
                WHERE apartment_id = %s
                """,
                (glat, glng, apartment_id),
            )
            updated += 1
        time.sleep(1.1)

    if not args.dry_run:
        conn.commit()
    conn.close()
    print(f"Updated {updated} rows.")
    if failed:
        print(f"Failed {len(failed)}:", file=sys.stderr)
        for item in failed:
            print(f"  {item[0]}: {item[1]}", file=sys.stderr)
        sys.exit(1 if updated == 0 else 0)


if __name__ == "__main__":
    main()
