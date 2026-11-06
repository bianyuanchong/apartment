"""
Daily Muse ↔ server pipeline (run once per day via cron / Task Scheduler).

  python scripts/daily_sync.py
  python scripts/daily_sync.py --date 2026-11-02
  python scripts/daily_sync.py --apartment-only
  python scripts/daily_sync.py --units-only

Steps:
  1. If S3 has apartment_list_muse_verified.json → upsert apartment.apartment
  2. Ingest latest (or --date) .../YYYY-MM-DD/unit_daily_feed.json → apartment.daily_feed
  3. Upload apartment_list.json from DB for Muse's next run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from modules.apartment_list_sync import (  # noqa: E402
    publish_apartment_list_for_muse,
    run_apartment_list_pipeline,
    sync_from_muse_verified_if_present,
)
from modules.unit_daily_feed_ingest import ingest_latest_unit_daily_feed  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily apartment catalog + unit feed sync")
    parser.add_argument(
        "--date",
        type=str,
        default="",
        help="Ingest unit_daily_feed for this YYYY-MM-DD (default: latest date folder on S3)",
    )
    parser.add_argument(
        "--apartment-only",
        action="store_true",
        help="Skip unit_daily_feed ingest",
    )
    parser.add_argument(
        "--units-only",
        action="store_true",
        help="Only ingest units; skip verified list + skip republish (testing)",
    )
    args = parser.parse_args()

    prefer: date | None = None
    if args.date:
        prefer = date.fromisoformat(args.date)

    result: dict = {}

    if args.units_only:
        unit_summary = ingest_latest_unit_daily_feed(prefer)
        result["unit_ingest"] = unit_summary or {"skipped": True, "reason": "no unit_daily_feed on S3"}
        print(json.dumps(result, indent=2))
        return

    if not args.apartment_only:
        unit_summary = ingest_latest_unit_daily_feed(prefer)
        result["unit_ingest"] = unit_summary or {
            "skipped": True,
            "reason": "no unit_daily_feed.json in date folder on S3",
        }

    if args.apartment_only:
        applied, stats = sync_from_muse_verified_if_present()
        result["verified_applied"] = applied
        if stats:
            result["apply_stats"] = stats.__dict__
        output_key = publish_apartment_list_for_muse()
        result["output_key"] = output_key
    else:
        catalog = run_apartment_list_pipeline()
        result.update(catalog)

    print(json.dumps(result, indent=2, default=str))

    unit = result.get("unit_ingest") or {}
    if unit.get("active_missing_count"):
        print("\n--- Active buildings NOT in today's feed ---", file=sys.stderr)
        for row in unit.get("active_missing_today") or []:
            last = row.get("last_feed_date") or "never"
            print(
                f"  #{row.get('apartment_id')} {row.get('name')} (last feed: {last})",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
