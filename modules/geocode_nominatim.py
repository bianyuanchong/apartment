"""Nominatim (OSM) geocoding helpers."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
DEFAULT_UA = "apartment-dashboard-geocode/1.0 (local dev; contact via repo owner)"


def _nyc_borough_label(zip_code: str) -> str:
    z = zip_code[:3]
    if z in {"104", "105"}:
        return "Bronx, NY"
    if z.startswith("112"):
        return "Brooklyn, NY"
    if z.startswith("113"):
        return "Queens, NY"
    if z.startswith("103"):
        return "Staten Island, NY"
    return "New York, NY"


def geocode_query(address: str | None, zip_code: str | None, name: str | None = None) -> str:
    z = (zip_code or "").strip()
    street = (address or "").strip()
    label = (name or "").strip()

    if z.startswith("073"):
        if street.lower() == "newport" or label.lower().startswith("newport"):
            return f"{label or 'Newport'}, Jersey City, NJ {z}, USA"
        if "/" in street:
            street = street.split("/")[0].strip()
        if street:
            return f"{street}, Jersey City, NJ {z}, USA"
        return f"Jersey City, NJ {z}, USA"

    if z.startswith(("10", "11")):
        borough = _nyc_borough_label(z)
        if street:
            return f"{street}, {borough} {z}, USA"
        if label:
            return f"{label}, {borough} {z}, USA"

    parts: list[str] = []
    if street and street.lower() not in ("newport", "none"):
        parts.append(street)
    elif label:
        parts.append(label)
    if z:
        parts.append(z)
    parts.append("USA")
    return ", ".join(p for p in parts if p)


NEWPORT_JC_CENTER = (40.7265, -74.0335)


def newport_building_coords(apartment_id: int) -> tuple[float, float]:
    """Shared Newport campus centroid + tiny per-building offset (Nominatim has no per-tower data)."""
    i = apartment_id % 97
    return (NEWPORT_JC_CENTER[0] + (i - 48) * 0.00008, NEWPORT_JC_CENTER[1] + (i - 48) * 0.00008)


def alternate_geocode_queries(
    address: str | None, zip_code: str | None, name: str | None
) -> list[str]:
    primary = geocode_query(address, zip_code, name)
    out = [primary]
    label = (name or "").strip()
    street = (address or "").strip()
    z = (zip_code or "").strip()

    if "northside piers" in label.lower():
        out.append(f"Northside Piers, Brooklyn, NY {z}, USA")
    if "rev james" in street.lower():
        out.append(
            street.replace("Rev James", "Reverend James A.")
            + f", Bronx, NY {z}, USA"
        )
    if z.startswith("073") and label.lower().startswith("newport"):
        out.append(f"401 Washington Boulevard, Jersey City, NJ {z}, USA")
    if z.startswith("104"):
        out.append(f"{z}, Bronx, NY, USA")
    return list(dict.fromkeys(out))


def geocode_apartment(
    *,
    apartment_id: int,
    name: str | None,
    address: str | None,
    zip_code: str | None,
    user_agent: str = DEFAULT_UA,
) -> tuple[float, float] | None:
    for query in alternate_geocode_queries(address, zip_code, name):
        try:
            coords = geocode_address(query, user_agent)
        except OSError:
            coords = None
        if coords is not None:
            z = (zip_code or "").strip()
            if z.startswith("073") and 40.65 <= coords[0] <= 40.85:
                return coords
            if not z.startswith("073"):
                return coords
    label = (name or "").strip()
    z = (zip_code or "").strip()
    if z.startswith("073") and label.lower().startswith("newport"):
        return newport_building_coords(apartment_id)
    return None


def geocode_address(query: str, user_agent: str = DEFAULT_UA) -> tuple[float, float] | None:
    params = urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1, "countrycodes": "us"}
    )
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}",
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])
