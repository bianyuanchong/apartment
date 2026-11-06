from __future__ import annotations

import math

# Approximate centroids for zips present in the portfolio (lat, lng).
ZIP_CENTROIDS: dict[str, tuple[float, float]] = {
    "08817": (40.518, -74.395),
    "08820": (40.559, -74.341),
    "08854": (40.548, -74.334),
    "08901": (40.486, -74.444),
    "10014": (40.734, -74.007),
    "10018": (40.755, -73.999),
    "10019": (40.765, -73.985),
    "10038": (40.709, -74.007),
    "10282": (40.716, -74.016),
    "11101": (40.748, -73.937),
    "11109": (40.747, -73.954),
    "11217": (40.683, -73.978),
}


def offset_from_id(apartment_id: int, zip_code: str | None) -> tuple[float, float]:
    """Spread markers within a zip when exact geocode is missing."""
    base = ZIP_CENTROIDS.get((zip_code or "").strip(), (40.75, -74.0))
    angle = (apartment_id * 137.508) % 360
    # ~0.5–1.5 km spread
    radius = 0.006 + (apartment_id % 5) * 0.0015
    lat = base[0] + radius * math.cos(math.radians(angle))
    lng = base[1] + radius * math.sin(math.radians(angle))
    return lat, lng


def resolve_coords(
    apartment_id: int,
    zip_code: str | None,
    latitude: float | None,
    longitude: float | None,
) -> tuple[float, float, str]:
    if latitude is not None and longitude is not None:
        return float(latitude), float(longitude), "geocoded"
    if zip_code and zip_code in ZIP_CENTROIDS:
        lat, lng = offset_from_id(apartment_id, zip_code)
        return lat, lng, "zip_estimate"
    return 40.75, -74.0, "default"
