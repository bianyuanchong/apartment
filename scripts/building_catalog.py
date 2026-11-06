from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).resolve().parent / "building_catalog.json"


@lru_cache
def load_catalog() -> dict[str, dict[str, Any]]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def enrich_building(building: dict[str, Any]) -> dict[str, Any]:
    """Apply canonical display name and street address from building_catalog.json."""
    building_id = building.get("id")
    if not building_id:
        return building

    meta = load_catalog().get(building_id)
    if not meta:
        return building

    out = dict(building)
    out["name"] = meta["name"]
    out["address"] = meta.get("address")
    if meta.get("zip"):
        out["zip"] = meta["zip"]
    return out
