from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings

ZCTA_PATH = Path(settings.BASE_DIR) / "dashboard" / "data" / "zcta_polygons.geojson"


def coverage_geojson(zip_stats: list[dict], *, include_untracked: bool = False) -> dict:
    """
    Merge portfolio stats into cached ZCTA polygons for map choropleth.

    zip_stats items: zip, apartment_count, unit_count, lat, lng (optional)

    When include_untracked is False (operator /app/ map), only ZIPs present in
    zip_stats are returned. When True (marketing landing), all regional ZCTA
    polygons are included with tracked=False for context.
    """
    stats_by_zip = {row["zip"]: row for row in zip_stats}
    if not ZCTA_PATH.is_file():
        return {"type": "FeatureCollection", "features": []}

    base = json.loads(ZCTA_PATH.read_text(encoding="utf-8"))
    features: list[dict] = []
    for feat in base.get("features", []):
        z = feat.get("properties", {}).get("zip")
        if not z:
            continue
        row = stats_by_zip.get(z)
        tracked = row is not None
        if not tracked and not include_untracked:
            continue
        props = {
            **feat.get("properties", {}),
            "zip": z,
            "tracked": tracked,
            "apartment_count": row["apartment_count"] if row else 0,
            "unit_count": row["unit_count"] if row else 0,
        }
        features.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": feat["geometry"],
            }
        )
    return {"type": "FeatureCollection", "features": features}
