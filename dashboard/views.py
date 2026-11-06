from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from dashboard.coverage import coverage_geojson
from dashboard.db import dictfetchone, query_all, query_one
from dashboard.geo import ZIP_CENTROIDS, resolve_coords
from dashboard.price_alerts import (
    DELETE_ALERT_SQL,
    INSERT_ALERT_SQL,
    RECENT_CHANGES_SQL,
    WATCHLIST_SQL,
    alert_triggered,
)

MAP_APARTMENTS_SQL = """
SELECT
    a.apartment_id,
    a.name,
    a.zip,
    a.address,
    a.latitude,
    a.longitude,
    COUNT(DISTINCT df.unit_number)::int AS unit_count
FROM apartment.apartment a
LEFT JOIN apartment.daily_feed df ON df.apartment_id = a.apartment_id
WHERE a.zip IS NOT NULL AND TRIM(a.zip) <> ''
GROUP BY a.apartment_id, a.name, a.zip, a.address, a.latitude, a.longitude
ORDER BY a.zip, a.name
"""

SUMMARY_SQL = """
SELECT
    a.zip,
    COUNT(DISTINCT a.apartment_id)::int AS apartment_count,
    COUNT(DISTINCT (df.apartment_id, df.unit_number))::int AS unit_count
FROM apartment.apartment a
LEFT JOIN apartment.daily_feed df ON df.apartment_id = a.apartment_id
WHERE a.zip IS NOT NULL AND TRIM(a.zip) <> ''
GROUP BY a.zip
ORDER BY a.zip
"""

APARTMENTS_BY_ZIP_SQL = """
SELECT
    a.apartment_id,
    a.name,
    a.address,
    a.url,
    a.last_feed_date,
    a.last_feed_unit_count,
    COUNT(DISTINCT df.unit_number)::int AS unit_count
FROM apartment.apartment a
LEFT JOIN apartment.daily_feed df ON df.apartment_id = a.apartment_id
WHERE a.zip = %s
GROUP BY a.apartment_id, a.name, a.address, a.url, a.last_feed_date, a.last_feed_unit_count
ORDER BY a.name
"""

APARTMENT_FEED_STATUS_SQL = """
SELECT
    apartment_id,
    name,
    is_active,
    last_feed_date,
    last_feed_unit_count
FROM apartment.apartment
ORDER BY apartment_id
"""

UNITS_BY_APARTMENT_SQL = """
SELECT
    df.unit_number,
    MAX(df.as_of_date) AS latest_seen_date,
    (ARRAY_AGG(df.rent_min ORDER BY df.as_of_date DESC))[1] AS latest_rent_min,
    (ARRAY_AGG(df.rent_max ORDER BY df.as_of_date DESC))[1] AS latest_rent_max,
    (ARRAY_AGG(df.bedrooms ORDER BY df.as_of_date DESC))[1] AS bedrooms,
    (ARRAY_AGG(df.bathrooms ORDER BY df.as_of_date DESC))[1] AS bathrooms
FROM apartment.daily_feed df
WHERE df.apartment_id = %s
GROUP BY df.unit_number
ORDER BY df.unit_number
"""

UNIT_HISTORY_SQL = """
SELECT
    df.as_of_date,
    df.rent_min,
    df.rent_max,
    df.availability,
    df.bedrooms,
    df.bathrooms,
    df.size_sqft
FROM apartment.daily_feed df
WHERE df.apartment_id = %s AND df.unit_number = %s
ORDER BY df.as_of_date
"""

APARTMENT_META_SQL = """
SELECT apartment_id, name, zip, address
FROM apartment.apartment
WHERE apartment_id = %s
"""


LANDING_BUILDINGS_SQL = """
SELECT a.name, a.zip, COUNT(DISTINCT df.unit_number)::int AS unit_count
FROM apartment.apartment a
LEFT JOIN apartment.daily_feed df ON df.apartment_id = a.apartment_id
GROUP BY a.apartment_id, a.name, a.zip
HAVING COUNT(DISTINCT df.unit_number) > 0
ORDER BY unit_count DESC
LIMIT 6
"""

LANDING_PINS_SQL = """
SELECT a.name, a.zip, a.latitude, a.longitude
FROM apartment.apartment a
WHERE a.latitude IS NOT NULL AND a.longitude IS NOT NULL
ORDER BY a.apartment_id
LIMIT 8
"""


def _map_pins_for_landing(apartments: list[dict]) -> list[dict]:
    if not apartments:
        return []
    lats = [float(r["latitude"]) for r in apartments]
    lngs = [float(r["longitude"]) for r in apartments]
    lat_min, lat_max = min(lats), max(lats)
    lng_min, lng_max = min(lngs), max(lngs)
    lat_span = lat_max - lat_min or 1.0
    lng_span = lng_max - lng_min or 1.0
    pins = []
    for row in apartments:
        lat = float(row["latitude"])
        lng = float(row["longitude"])
        pins.append(
            {
                "name": row["name"],
                "zip": row["zip"],
                "left_pct": round(12 + (lng - lng_min) / lng_span * 76, 2),
                "top_pct": round(15 + (1 - (lat - lat_min) / lat_span) * 68, 2),
            }
        )
    return pins


@require_GET
def landing(request):
    """Client-facing marketing page (operator app lives at /app/)."""
    empty = {
        "stats": {"zip_count": 0, "apartment_count": 0, "unit_count": 0, "latest_date": None},
        "top_zips": [],
        "sample_buildings": [],
        "price_moves": [],
        "map_pins": [],
        "featured_chart": [],
        "featured_chart_label": "",
    }
    try:
        rows = query_all(SUMMARY_SQL)
        latest = query_one("SELECT MAX(as_of_date) AS d FROM apartment.daily_feed")
        stats = {
            "zip_count": len(rows),
            "apartment_count": sum(r["apartment_count"] for r in rows),
            "unit_count": sum(r["unit_count"] for r in rows),
            "latest_date": latest["d"] if latest else None,
        }
        top_zips = sorted(rows, key=lambda r: r["unit_count"], reverse=True)[:5]
        sample_buildings = query_all(LANDING_BUILDINGS_SQL)
        price_moves = query_all(RECENT_CHANGES_SQL)[:5]
        map_pins = _map_pins_for_landing(query_all(LANDING_PINS_SQL))

        featured_chart: list[dict] = []
        featured_chart_label = ""
        featured_chart_series: list[dict] = []
        if price_moves:
            lead = price_moves[0]
            featured_chart_label = f"{lead['unit_number']} · {lead['apartment_name']}"
            history = query_all(
                UNIT_HISTORY_SQL,
                [lead["apartment_id"], lead["unit_number"]],
            )
            featured_chart = history[-10:]
            for h in featured_chart:
                if h.get("rent_min") is None:
                    continue
                featured_chart_series.append(
                    {
                        "date": h["as_of_date"].isoformat()
                        if hasattr(h["as_of_date"], "isoformat")
                        else str(h["as_of_date"]),
                        "rent_min": float(h["rent_min"]),
                    }
                )

        map_pins_geo = [
            {
                "name": r["name"],
                "zip": r["zip"],
                "lat": float(r["latitude"]),
                "lng": float(r["longitude"]),
            }
            for r in query_all(LANDING_PINS_SQL)
        ]

        return render(
            request,
            "dashboard/landing.html",
            {
                "stats": stats,
                "top_zips": top_zips,
                "sample_buildings": sample_buildings,
                "price_moves": price_moves,
                "map_pins": map_pins,
                "map_pins_geo": map_pins_geo,
                "coverage_geojson": coverage_geojson(rows, include_untracked=True),
                "carto_tile_url": settings.CARTO_RASTER_TILE_URL,
                "featured_chart": featured_chart,
                "featured_chart_label": featured_chart_label,
                "featured_chart_series": featured_chart_series,
            },
        )
    except Exception:
        return render(
            request,
            "dashboard/landing.html",
            {
                **empty,
                "map_pins_geo": [],
                "coverage_geojson": {"type": "FeatureCollection", "features": []},
                "carto_tile_url": settings.CARTO_RASTER_TILE_URL,
                "featured_chart_series": [],
            },
        )


@ensure_csrf_cookie
@require_GET
def index(request):
    return render(
        request,
        "dashboard/index.html",
        {"carto_tile_url": settings.CARTO_RASTER_TILE_URL},
    )


@require_GET
def api_map(request):
    summary = query_all(SUMMARY_SQL)
    apartments = query_all(MAP_APARTMENTS_SQL)
    apt_features = []
    for row in apartments:
        lat, lng, loc_source = resolve_coords(
            row["apartment_id"],
            row["zip"],
            row["latitude"],
            row["longitude"],
        )
        apt_features.append(
            {
                **row,
                "lat": lat,
                "lng": lng,
                "location_source": loc_source,
            }
        )

    geocoded_by_zip: dict[str, list[tuple[float, float]]] = {}
    for apt in apt_features:
        if apt["location_source"] != "geocoded":
            continue
        geocoded_by_zip.setdefault(apt["zip"], []).append((apt["lat"], apt["lng"]))

    zip_features = []
    for row in summary:
        z = row["zip"]
        pts = geocoded_by_zip.get(z)
        if pts:
            lat = sum(p[0] for p in pts) / len(pts)
            lng = sum(p[1] for p in pts) / len(pts)
        elif z in ZIP_CENTROIDS:
            lat, lng = ZIP_CENTROIDS[z]
        else:
            continue
        zip_features.append(
            {
                "zip": z,
                "lat": lat,
                "lng": lng,
                "apartment_count": row["apartment_count"],
                "unit_count": row["unit_count"],
            }
        )

    return JsonResponse(
        {
            "zips": zip_features,
            "apartments": apt_features,
            "coverage": coverage_geojson(
                [r for r in summary if r["unit_count"] > 0],
            ),
            "note": "Apartments without latitude/longitude use zip centroid + offset until geocoded.",
        },
        encoder=DjangoJSONEncoder,
    )


def _feed_health() -> dict:
    latest = query_one("SELECT MAX(as_of_date) AS d FROM apartment.daily_feed")
    latest_date = latest["d"] if latest and latest.get("d") else None
    buildings = query_all(APARTMENT_FEED_STATUS_SQL)
    active = [b for b in buildings if b.get("is_active")]
    stale_active: list[dict] = []
    updated_on_latest = 0
    for b in active:
        if latest_date and b.get("last_feed_date") == latest_date:
            updated_on_latest += 1
        else:
            stale_active.append(
                {
                    "apartment_id": b["apartment_id"],
                    "name": b["name"],
                    "last_feed_date": b.get("last_feed_date"),
                    "last_feed_unit_count": b.get("last_feed_unit_count"),
                }
            )
    return {
        "latest_feed_date": latest_date,
        "active_count": len(active),
        "updated_on_latest": updated_on_latest,
        "stale_active_count": len(stale_active),
        "stale_active": stale_active,
        "buildings": buildings,
    }


@require_GET
def api_summary(request):
    rows = query_all(SUMMARY_SQL)
    total_apartments = sum(r["apartment_count"] for r in rows)
    total_units = sum(r["unit_count"] for r in rows)
    return JsonResponse(
        {
            "zip_count": len(rows),
            "apartment_count": total_apartments,
            "unit_count": total_units,
            "by_zip": rows,
            "feed_health": _feed_health(),
        },
        encoder=DjangoJSONEncoder,
    )


@require_GET
def api_zip_apartments(request, zip_code: str):
    rows = query_all(APARTMENTS_BY_ZIP_SQL, [zip_code])
    if not rows:
        exists = query_one(
            "SELECT 1 FROM apartment.apartment WHERE zip = %s LIMIT 1", [zip_code]
        )
        if not exists:
            raise Http404("Zip not found")
    return JsonResponse({"zip": zip_code, "apartments": rows}, encoder=DjangoJSONEncoder)


@require_GET
def api_apartment_units(request, apartment_id: int):
    meta = query_one(APARTMENT_META_SQL, [apartment_id])
    if not meta:
        raise Http404("Apartment not found")
    units = query_all(UNITS_BY_APARTMENT_SQL, [apartment_id])
    return JsonResponse({"apartment": meta, "units": units}, encoder=DjangoJSONEncoder)


@require_GET
def api_unit_history(request, apartment_id: int):
    unit_number = request.GET.get("unit_number", "").strip()
    if not unit_number:
        return JsonResponse({"error": "unit_number required"}, status=400)
    meta = query_one(APARTMENT_META_SQL, [apartment_id])
    if not meta:
        raise Http404("Apartment not found")
    history = query_all(UNIT_HISTORY_SQL, [apartment_id, unit_number])
    return JsonResponse(
        {
            "apartment": meta,
            "unit_number": unit_number,
            "history": history,
        },
        encoder=DjangoJSONEncoder,
    )


@require_http_methods(["GET", "POST"])
def api_price_alerts(request):
    if request.method == "GET":
        changes = query_all(RECENT_CHANGES_SQL)
        watchlist = query_all(WATCHLIST_SQL)
        triggered = []
        for row in watchlist:
            if alert_triggered(row["direction"], row["target_rent"], row["latest_rent_min"]):
                triggered.append(row)
        latest_date = changes[0]["latest_date"] if changes else None
        prev_date = changes[0]["prev_date"] if changes else None
        return JsonResponse(
            {
                "latest_date": latest_date,
                "prev_date": prev_date,
                "changes": changes,
                "watchlist": watchlist,
                "triggered": triggered,
                "triggered_count": len(triggered),
            },
            encoder=DjangoJSONEncoder,
        )

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    apartment_id = payload.get("apartment_id")
    unit_number = (payload.get("unit_number") or "").strip()
    direction = (payload.get("direction") or "below").strip()
    note = payload.get("note")

    if direction not in ("below", "above"):
        return JsonResponse({"error": "direction must be below or above"}, status=400)
    if not apartment_id or not unit_number:
        return JsonResponse({"error": "apartment_id and unit_number required"}, status=400)

    try:
        target_rent = Decimal(str(payload.get("target_rent")))
    except (InvalidOperation, TypeError):
        return JsonResponse({"error": "target_rent required"}, status=400)
    if target_rent <= 0:
        return JsonResponse({"error": "target_rent must be positive"}, status=400)

    meta = query_one(APARTMENT_META_SQL, [apartment_id])
    if not meta:
        raise Http404("Apartment not found")

    with connection.cursor() as cursor:
        cursor.execute(
            INSERT_ALERT_SQL,
            [apartment_id, unit_number, target_rent, direction, note],
        )
        row = dictfetchone(cursor)
    return JsonResponse({"alert": row}, status=201, encoder=DjangoJSONEncoder)


@require_http_methods(["DELETE"])
def api_price_alert_delete(request, alert_id: int):
    with connection.cursor() as cursor:
        cursor.execute(DELETE_ALERT_SQL, [alert_id])
        row = dictfetchone(cursor)
    if not row:
        raise Http404("Alert not found")
    return JsonResponse({"deleted": alert_id})
