"""
Insert new buildings from nyc_apartments.csv into apartment.apartment (no truncate).

Does not change existing rows unless --update-matching-name.

Expected columns (flexible headers, case-insensitive):
  name / building / building_name / property_name  (required)
  url / website / listing_url / scrape_url       (optional → pending:nyc-{slug})
  address / full_address                         (optional)
  zip / zipcode / postal_code                    (optional)

Usage (repo root):
  python scripts/import_nyc_apartments_csv.py data/nyc_apartments.csv
  python scripts/import_nyc_apartments_csv.py --dry-run path/to/nyc_apartments.csv
  python scripts/import_nyc_apartments_csv.py --publish path/to/nyc_apartments.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from modules.apartment_list_sync import publish_apartment_list_for_muse  # noqa: E402
from modules.database_utils import db_connect  # noqa: E402

DEFAULT_CSV = REPO_ROOT / "data" / "nyc_apartments.csv"
JERSEY_DELTA_CSV = REPO_ROOT / "data" / "jersey_city_apartment_list_delta.csv"


def pending_prefix_for_path(path: Path) -> str:
    name = path.name.lower()
    if "jersey" in name:
        return "jersey"
    if "nyc" in name:
        return "nyc"
    return "catalog"

ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "building", "building_name", "property_name", "apartment_name", "title"),
    "url": ("url", "website", "listing_url", "scrape_url", "availability_url", "link"),
    "address": ("address", "full_address", "street_address", "street"),
    "zip": ("zip", "zipcode", "zip_code", "postal_code", "postal"),
    "is_active": ("is_active", "active"),
}


def _field_map(row: dict[str, str]) -> dict[str, str]:
    lower = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
    out: dict[str, str] = {}
    for canonical, keys in ALIASES.items():
        for key in keys:
            if key in lower and lower[key]:
                out[canonical] = lower[key]
                break
    return out


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:72] or "building"


def parse_zip(raw: str) -> str | None:
    if not raw:
        return None
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", raw)
    return m.group(1) if m else raw[:10] or None


def load_rows(path: Path, *, pending_prefix: str = "nyc") -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    parsed: list[dict] = []
    for i, raw in enumerate(reader, start=2):
        fields = _field_map(raw)
        name = fields.get("name")
        if not name:
            print(f"  skip line {i}: missing name", file=sys.stderr)
            continue
        url = fields.get("url") or f"pending:{pending_prefix}-{slugify(name)}"
        zip_val = parse_zip(fields.get("zip", ""))
        if not zip_val and fields.get("address"):
            zip_val = parse_zip(fields["address"])
        active_raw = fields.get("is_active", "true").lower()
        is_active = active_raw not in ("0", "false", "no", "n")
        parsed.append(
            {
                "name": name,
                "url": url,
                "address": fields.get("address") or None,
                "zip": zip_val,
                "is_active": is_active,
            }
        )
    return parsed


def existing_names_and_urls(cur) -> tuple[set[str], set[str]]:
    cur.execute("SELECT name, url FROM apartment.apartment")
    names: set[str] = set()
    urls: set[str] = set()
    for name, url in cur.fetchall():
        names.add(name.strip().lower())
        urls.add(str(url).strip().lower())
    return names, urls


def main() -> None:
    parser = argparse.ArgumentParser(description="Import NYC apartments CSV into apartment.apartment")
    parser.add_argument(
        "csv_path",
        nargs="?",
        default=str(DEFAULT_CSV),
        help=f"Path to CSV (default: {DEFAULT_CSV})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse and print actions only")
    parser.add_argument(
        "--update-matching-name",
        action="store_true",
        help="Update url/address/zip when name already exists",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="After import, upload apartment_list.json to S3 for Muse",
    )
    parser.add_argument(
        "--pending-prefix",
        type=str,
        default="",
        help="Slug for pending URLs (default: auto from filename, e.g. jersey / nyc)",
    )
    parser.add_argument(
        "--all-catalog-deltas",
        action="store_true",
        help=f"Import {DEFAULT_CSV.name} and {JERSEY_DELTA_CSV.name} if present",
    )
    args = parser.parse_args()

    if args.all_catalog_deltas:
        _run_import_batch(
            dry_run=args.dry_run,
            update_matching_name=args.update_matching_name,
            publish=args.publish,
        )
        return

    path = Path(args.csv_path)
    if not path.is_file():
        path = REPO_ROOT / args.csv_path
    if not path.is_file():
        print(
            f"CSV not found: {args.csv_path}\n"
            "Copy CSV from Google Drive to data/ (e.g.\n"
            f"  {DEFAULT_CSV}\n"
            f"  {JERSEY_DELTA_CSV}\n"
            ") or pass the full local path.",
            file=sys.stderr,
        )
        sys.exit(1)

    prefix = args.pending_prefix.strip() or pending_prefix_for_path(path)
    rows = load_rows(path, pending_prefix=prefix)
    if not rows:
        print("No importable rows.", file=sys.stderr)
        sys.exit(1)

    summary = apply_import(
        rows,
        path=path,
        dry_run=args.dry_run,
        update_matching_name=args.update_matching_name,
    )
    print(json.dumps(summary, indent=2))

    if args.publish and not args.dry_run and (summary["inserted"] or summary["updated"]):
        key = publish_apartment_list_for_muse()
        print(f"Published {key}")
    return


def _run_import_batch(*, dry_run: bool, update_matching_name: bool, publish: bool) -> None:
    paths = [DEFAULT_CSV, JERSEY_DELTA_CSV]
    totals = {"inserted": 0, "updated": 0, "skipped": 0, "parsed": 0}
    any_work = False
    for path in paths:
        if not path.is_file():
            print(f"skip missing {path}", file=sys.stderr)
            continue
        prefix = pending_prefix_for_path(path)
        rows = load_rows(path, pending_prefix=prefix)
        print(f"\n=== {path.name} ({len(rows)} rows, prefix={prefix}) ===", file=sys.stderr)
        summary = apply_import(
            rows,
            path=path,
            dry_run=dry_run,
            update_matching_name=update_matching_name,
        )
        print(json.dumps(summary, indent=2))
        for k in totals:
            totals[k] += summary.get(k, 0)
        if summary.get("inserted") or summary.get("updated"):
            any_work = True
    print(json.dumps({"batch_totals": totals}, indent=2))
    if publish and not dry_run and any_work:
        key = publish_apartment_list_for_muse()
        print(f"Published {key}")


def apply_import(
    rows: list[dict],
    *,
    path: Path,
    dry_run: bool,
    update_matching_name: bool,
) -> dict:
    conn, cur = db_connect()
    try:
        names_seen, urls_seen = existing_names_and_urls(cur)
        inserted = updated = skipped = 0

        for row in rows:
            key = row["name"].strip().lower()
            url_key = row["url"].strip().lower()
            if key in names_seen:
                if update_matching_name:
                    if dry_run:
                        print(f"UPDATE {row['name']!r}")
                        updated += 1
                        continue
                    cur.execute(
                        """
                        UPDATE apartment.apartment
                        SET url = %s, address = %s, zip = %s, is_active = %s, updated_at = NOW()
                        WHERE LOWER(name) = LOWER(%s)
                        """,
                        (row["url"], row["address"], row["zip"], row["is_active"], row["name"]),
                    )
                    updated += cur.rowcount
                else:
                    print(f"  skip duplicate name: {row['name']}", file=sys.stderr)
                    skipped += 1
                continue
            if dry_run:
                print(f"INSERT {row['name']!r} zip={row['zip']} url={row['url']}")
                inserted += 1
                names_seen.add(key)
                urls_seen.add(url_key)
                continue

            cur.execute(
                """
                INSERT INTO apartment.apartment (name, url, is_active, address, zip)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING apartment_id
                """,
                (row["name"], row["url"], row["is_active"], row["address"], row["zip"]),
            )
            apt_id = cur.fetchone()[0]
            print(f"inserted #{apt_id} {row['name']}")
            inserted += 1
            names_seen.add(key)
            urls_seen.add(url_key)

        if not dry_run:
            conn.commit()

        return {
            "csv": str(path),
            "parsed": len(rows),
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "dry_run": dry_run,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    main()
