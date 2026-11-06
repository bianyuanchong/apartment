"""
Refresh address/zip from catalog + CSVs, then geocode rows missing lat/lng.

  python scripts/backfill_apartment_addresses_geocode.py
  python scripts/backfill_apartment_addresses_geocode.py --dry-run
  python scripts/backfill_apartment_addresses_geocode.py --limit 20
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from building_catalog import load_catalog  # noqa: E402
from import_nyc_apartments_csv import (  # noqa: E402
    DEFAULT_CSV,
    JERSEY_DELTA_CSV,
    load_rows,
)
from modules.database_utils import db_connect  # noqa: E402
from modules.geocode_nominatim import DEFAULT_UA, geocode_apartment, geocode_query  # noqa: E402


def address_by_name_from_csvs() -> dict[str, dict[str, str | None]]:
    """Lowercase name -> {address, zip} (Jersey overwrites NYC on duplicate names — unlikely)."""
    out: dict[str, dict[str, str | None]] = {}
    for path, prefix in ((DEFAULT_CSV, "nyc"), (JERSEY_DELTA_CSV, "jersey")):
        if not path.is_file():
            continue
        for row in load_rows(path, pending_prefix=prefix):
            key = row["name"].strip().lower()
            out[key] = {"address": row.get("address"), "zip": row.get("zip")}
    return out


def refresh_addresses(cur, target_ids: set[int]) -> dict[str, int]:
    stats = {"catalog_by_name": 0, "catalog_by_pending": 0, "csv_by_name": 0, "unchanged": 0}
    if not target_ids:
        return stats

    catalog = load_catalog()
    csv_map = address_by_name_from_csvs()

    cur.execute(
        """
        SELECT apartment_id, name, url, address, zip
        FROM apartment.apartment
        WHERE apartment_id = ANY(%s)
        ORDER BY apartment_id
        """,
        (list(target_ids),),
    )
    rows = cur.fetchall()

    for apartment_id, name, url, old_address, old_zip in rows:
        new_address: str | None = None
        new_zip: str | None = None
        source = None

        slug = str(url).removeprefix("pending:") if str(url).startswith("pending:") else None
        if slug and slug in catalog:
            meta = catalog[slug]
            new_address = meta.get("address")
            new_zip = meta.get("zip")
            source = "catalog_by_pending"

        if source is None:
            for _slug, meta in catalog.items():
                if str(meta.get("name", "")).strip().lower() == str(name).strip().lower():
                    new_address = meta.get("address")
                    new_zip = meta.get("zip")
                    source = "catalog_by_name"
                    break

        if source is None:
            csv_row = csv_map.get(str(name).strip().lower())
            if csv_row:
                csv_addr = (csv_row.get("address") or "").strip()
                if csv_addr.lower() != "newport" or not str(old_zip or "").startswith("073"):
                    new_address = csv_row.get("address")
                    new_zip = csv_row.get("zip")
                    source = "csv_by_name"

        if source is None:
            stats["unchanged"] += 1
            continue

        if new_address == old_address and new_zip == old_zip:
            stats["unchanged"] += 1
            continue

        cur.execute(
            """
            UPDATE apartment.apartment
            SET address = %s, zip = %s, updated_at = NOW()
            WHERE apartment_id = %s
            """,
            (new_address, new_zip, apartment_id),
        )
        stats[source] += 1

    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Max geocode attempts (0 = all missing)")
    parser.add_argument(
        "--skip-address-refresh",
        action="store_true",
        help="Only geocode; do not update address/zip from catalog/CSV",
    )
    args = parser.parse_args()

    user_agent = os.environ.get("NOMINATIM_USER_AGENT", DEFAULT_UA)

    conn, cur = db_connect()
    try:
        cur.execute(
            """
            SELECT apartment_id
            FROM apartment.apartment
            WHERE latitude IS NULL OR longitude IS NULL
               OR (zip LIKE '073%%' AND (latitude < 40.65 OR latitude > 40.85))
            ORDER BY apartment_id
            """
        )
        target_ids = {int(row[0]) for row in cur.fetchall()}

        if not args.skip_address_refresh:
            refresh_stats = refresh_addresses(cur, target_ids)
            print("Address refresh:", refresh_stats)
            if not args.dry_run:
                conn.commit()

        cur.execute(
            """
            SELECT apartment_id, name, address, zip
            FROM apartment.apartment
            WHERE apartment_id = ANY(%s)
            ORDER BY apartment_id
            """,
            (list(target_ids),),
        )
        rows = cur.fetchall()

        updated = 0
        failed: list[tuple[int, str]] = []
        attempted = 0

        for apartment_id, name, address, zip_code in rows:
            if args.limit and attempted >= args.limit:
                break
            attempted += 1

            query = geocode_query(address, zip_code, name=name)
            if not query.replace(",", "").strip():
                failed.append((apartment_id, f"{name}: empty geocode query"))
                continue

            if args.dry_run:
                print(f"DRY\t{apartment_id}\t{name}\t{query}")
                continue

            try:
                coords = geocode_apartment(
                    apartment_id=apartment_id,
                    name=name,
                    address=address,
                    zip_code=zip_code,
                    user_agent=user_agent,
                )
            except Exception as exc:  # noqa: BLE001
                failed.append((apartment_id, f"{name}: {exc}"))
                time.sleep(1.1)
                continue

            if coords is None:
                failed.append((apartment_id, f"{name}: no result for {query!r}"))
                time.sleep(1.1)
                continue

            glat, glng = coords
            print(f"{apartment_id}\t{name}\t{glat:.6f},{glng:.6f}\t{query}", flush=True)
            cur.execute(
                """
                UPDATE apartment.apartment
                SET latitude = %s, longitude = %s, updated_at = NOW()
                WHERE apartment_id = %s
                """,
                (glat, glng, apartment_id),
            )
            conn.commit()
            updated += 1
            time.sleep(1.1)

        print(f"Geocoded {updated} rows (attempted {attempted}).")
        if failed:
            print(f"Failed {len(failed)}:", file=sys.stderr)
            for apt_id, msg in failed[:30]:
                print(f"  {apt_id}: {msg}", file=sys.stderr)
            if len(failed) > 30:
                print(f"  ... and {len(failed) - 30} more", file=sys.stderr)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
