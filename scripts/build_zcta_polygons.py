"""
Fetch 2020 Census ZCTA polygons for zips in apartment.apartment and cache GeoJSON.

Usage (repo root):
  python scripts/build_zcta_polygons.py
  python scripts/build_zcta_polygons.py --extra 10001   # include zip not yet in DB

Output: dashboard/data/zcta_polygons.geojson
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg2

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / "dashboard" / "data" / "zcta_polygons.geojson"
TIGER_QUERY = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1/query"
)


def iter_coords(geometry: dict):
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        for ring in coords:
            for lng, lat in ring:
                yield lng, lat
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                for lng, lat in ring:
                    yield lng, lat


def bbox_from_features(features: list[dict], pad: float = 0.12) -> tuple[float, float, float, float]:
    lngs: list[float] = []
    lats: list[float] = []
    for feat in features:
        geom = feat.get("geometry")
        if not geom:
            continue
        for lng, lat in iter_coords(geom):
            lngs.append(lng)
            lats.append(lat)
    if not lngs:
        raise ValueError("No coordinates to bound")
    xmin, xmax = min(lngs), max(lngs)
    ymin, ymax = min(lats), max(lats)
    return xmin - pad, ymin - pad, xmax + pad, ymax + pad


def fetch_zctas_in_bbox(xmin: float, ymin: float, xmax: float, ymax: float) -> list[dict]:
    params = urllib.parse.urlencode(
        {
            "where": "1=1",
            "geometry": f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "ZCTA5,NAME",
            "f": "geojson",
            "outSR": "4326",
            "returnGeometry": "true",
            "resultRecordCount": "2000",
        }
    )
    url = f"{TIGER_QUERY}?{params}"
    with urllib.request.urlopen(url, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out: list[dict] = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        z = str(props.get("ZCTA5") or props.get("zcta5") or "").strip()
        if not z:
            continue
        feat["properties"] = {"zip": z, "zcta_name": props.get("NAME") or f"ZCTA5 {z}"}
        out.append(feat)
    return out


def fetch_zcta(zip_code: str) -> dict | None:
    params = urllib.parse.urlencode(
        {
            "where": f"ZCTA5='{zip_code}'",
            "outFields": "ZCTA5,NAME",
            "f": "geojson",
            "outSR": "4326",
            "maxAllowableOffset": "0.0008",
        }
    )
    url = f"{TIGER_QUERY}?{params}"
    with urllib.request.urlopen(url, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    features = data.get("features") or []
    if not features:
        return None
    feat = features[0]
    feat["properties"] = {"zip": zip_code, "zcta_name": feat.get("properties", {}).get("NAME")}
    return feat


def distinct_zips_from_db() -> list[str]:
    creds = json.loads((REPO_ROOT / "creds" / "database.json").read_text(encoding="utf-8"))[
        "localhost"
    ]
    conn = psycopg2.connect(**creds)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT zip FROM apartment.apartment
        WHERE zip IS NOT NULL AND TRIM(zip) <> ''
        ORDER BY zip
        """
    )
    zips = [row[0].strip() for row in cur.fetchall()]
    conn.close()
    return zips


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extra", action="append", default=[], help="Additional ZIP codes to fetch")
    parser.add_argument(
        "--region",
        action="store_true",
        help="After seed zips load, fetch all ZCTA polygons in the surrounding bbox (for map context)",
    )
    parser.add_argument(
        "--pad",
        type=float,
        default=0.12,
        help="BBox padding in degrees when using --region (default 0.12)",
    )
    args = parser.parse_args()

    zips = sorted(set(distinct_zips_from_db()) | set(args.extra))
    if not zips:
        print("No zips found.", file=sys.stderr)
        sys.exit(1)

    existing: dict[str, dict] = {}
    if OUT_PATH.is_file():
        for feat in json.loads(OUT_PATH.read_text(encoding="utf-8")).get("features", []):
            z = feat.get("properties", {}).get("zip")
            if z:
                existing[z] = feat

    features: list[dict] = []
    failed: list[str] = []
    for zip_code in zips:
        if zip_code in existing:
            features.append(existing[zip_code])
            print(f"{zip_code}\tcached")
            continue
        try:
            feat = fetch_zcta(zip_code)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{zip_code}: {exc}")
            time.sleep(0.5)
            continue
        if feat is None:
            failed.append(f"{zip_code}: not in TIGER ZCTA")
            time.sleep(0.5)
            continue
        features.append(feat)
        print(f"{zip_code}\tfetched")
        time.sleep(0.35)

    by_zip = {f["properties"]["zip"]: f for f in features if f.get("properties", {}).get("zip")}

    if args.region and features:
        xmin, ymin, xmax, ymax = bbox_from_features(features, pad=args.pad)
        print(f"Region bbox: {xmin:.4f},{ymin:.4f} {xmax:.4f},{ymax:.4f}")
        try:
            regional = fetch_zctas_in_bbox(xmin, ymin, xmax, ymax)
        except Exception as exc:  # noqa: BLE001
            print(f"Region fetch failed: {exc}", file=sys.stderr)
            regional = []
        for feat in regional:
            z = feat.get("properties", {}).get("zip")
            if z:
                by_zip.setdefault(z, feat)
        print(f"Region fill: {len(regional)} from TIGER, {len(by_zip)} total zips")

    merged = [by_zip[k] for k in sorted(by_zip.keys())]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps({"type": "FeatureCollection", "features": merged}, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Wrote {len(merged)} polygons to {OUT_PATH}")
    if failed:
        print("Failed:", file=sys.stderr)
        for line in failed:
            print(f"  {line}", file=sys.stderr)


if __name__ == "__main__":
    main()
