"""
Split merged daily JSON (grouped by operator/property) into per-building records.

Usage:
  python scripts/merged_group_by_building.py merged_2026-11-01.json
  python scripts/merged_group_by_building.py merged_2026-11-01.json -o out.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent

import sys

sys.path.insert(0, str(SCRIPTS_DIR))
from building_catalog import enrich_building  # noqa: E402


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "building"


def strip_unit_building_key(unit: dict) -> dict:
    out = {k: v for k, v in unit.items() if k != "building"}
    return out


def mode_zip(units: list[dict]) -> str | None:
    zips = [u.get("_zip") for u in units if u.get("_zip")]
    if not zips:
        return None
    return Counter(zips).most_common(1)[0][0]


def expand_property(prop: dict) -> list[dict]:
    units = prop.get("units") or []
    floorplans = prop.get("floorplans") or []
    base = {
        "parent_property_id": prop["id"],
        "parent_property_name": prop.get("property"),
        "source": prop.get("source"),
        "snapshot_date": prop.get("snapshot_date"),
    }

    by_building: dict[str, list[dict]] = {}
    for unit in units:
        key = unit.get("building") or prop.get("property") or prop["id"]
        by_building.setdefault(key, []).append(unit)

    if not by_building:
        by_building[prop.get("property") or prop["id"]] = []

    buildings: list[dict] = []
    multi = len(by_building) > 1

    for building_name, building_units in sorted(by_building.items(), key=lambda x: x[0]):
        if multi:
            building_id = f"{prop['id']}--{slugify(str(building_name))}"
            display_name = str(building_name)
        else:
            building_id = prop["id"]
            display_name = prop.get("property") or str(building_name)

        zip_code = mode_zip(building_units) or prop.get("zip")
        address = prop.get("address") or None
        if multi and (not address or address in ("", "Edison, NJ", "Long Island City, NY")):
            # Operator-level address is vague; keep zip from units when we have it.
            pass

        entry = {
            **base,
            "id": building_id,
            "name": display_name,
            "address": address,
            "zip": zip_code,
            "units": [strip_unit_building_key(u) for u in building_units],
            "floorplans": deepcopy(floorplans) if not multi else [],
        }
        if multi:
            entry["floorplans_note"] = (
                "Floorplans in source merged file are operator-level; "
                "not split per building."
            )
        buildings.append(enrich_building(entry))

    return buildings


def transform(merged: dict) -> dict:
    buildings: list[dict] = []
    for prop in merged.get("properties") or []:
        buildings.extend(expand_property(prop))

    unit_count = sum(len(b.get("units") or []) for b in buildings)
    return {
        "scraped_date": merged.get("scraped_date"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_file": merged.get("generated_at"),
        "building_count": len(buildings),
        "unit_count": unit_count,
        "buildings": buildings,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("merged_json", type=Path)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output path (default: <stem>_by_building.json beside input)",
    )
    args = parser.parse_args()

    merged_path = args.merged_json
    if not merged_path.is_file():
        merged_path = REPO_ROOT / args.merged_json

    merged = json.loads(merged_path.read_text(encoding="utf-8"))
    out = transform(merged)

    out_path = args.output
    if out_path is None:
        out_path = merged_path.with_name(f"{merged_path.stem}_by_building.json")

    out_path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} ({out['building_count']} buildings, {out['unit_count']} units)")


if __name__ == "__main__":
    main()
