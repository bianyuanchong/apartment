"""
Ingest merged_YYYY-MM-DD.json into apartment.daily_feed (maps building slug → apartment_id).

  python scripts/ingest_merged_daily_feed.py --all-s3
  python scripts/ingest_merged_daily_feed.py merged_2026-11-01.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from modules.daily_feed_ingest import (  # noqa: E402
    ingest_all_s3_merged,
    ingest_merged_from_s3,
    ingest_merged_payload,
    list_s3_merged_keys,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("merged_json", nargs="?", help="Local merged JSON path")
    parser.add_argument("--all-s3", action="store_true", help="Ingest every merged_*.json on S3")
    parser.add_argument("--s3-key", help="Single S3 key under daily_feed_json/...")
    args = parser.parse_args()

    summaries: list[dict] = []

    if args.all_s3:
        summaries = ingest_all_s3_merged()
    elif args.s3_key:
        summaries = [ingest_merged_from_s3(args.s3_key)]
    elif args.merged_json:
        path = Path(args.merged_json)
        if not path.is_file():
            path = REPO_ROOT / args.merged_json
        merged = json.loads(path.read_text(encoding="utf-8"))
        summaries = [ingest_merged_payload(merged, source_label=str(path))]
    else:
        parser.error("Provide merged_json, --all-s3, or --s3-key")

    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
