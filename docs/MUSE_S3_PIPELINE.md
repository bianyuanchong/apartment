# Apartment daily pipeline — S3 contract (Muse ↔ Server)

This document describes how **Muse** and the **apartment server** exchange files on S3 each day. The goal is a closed loop: Muse scrapes from a canonical apartment list, writes verified metadata and unit snapshots; the server ingests into PostgreSQL and publishes the next day’s source list.

---

## S3 location

| Item | Value |
|------|--------|
| Region | `us-west-2` (US West Oregon) |
| Bucket | `gavin-apartment` |
| Prefix | `daily_feed_json/` |
| Base URI | `s3://gavin-apartment/daily_feed_json/` |

Credentials are exchanged out of band (not in this doc).

---

## Daily cycle (overview)

```text
                    ┌─────────────────────────────────────────┐
                    │  s3://.../daily_feed_json/              │
                    │  apartment_list.json  ◄── server output │
                    └─────────────────┬───────────────────────┘
                                      │ read (today's source)
                                      ▼
                              ┌───────────────┐
                              │     Muse      │
                              │ verify URLs   │
                              │ scrape units  │
                              └───────┬───────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          ▼                           ▼                           │
 apartment_list_muse_verified.json   YYYY-MM-DD/                    │
 (prefix root)                      unit_daily_feed.json           │
          │                           │                           │
          ▼                           ▼                           │
   ┌──────────────┐            ┌──────────────┐                    │
   │   Server     │  ingest    │  PostgreSQL  │                    │
   │  daily job   │ ─────────► │ apartment +  │                    │
   │              │            │ daily_feed   │                    │
   └──────┬───────┘            └──────────────┘                    │
          │                                                         │
          └──────── writes fresh apartment_list.json ───────────────┘
                    (tomorrow's Muse source)
```

**Order of operations (server side, once per day):**

1. If `apartment_list_muse_verified.json` exists → download and apply updates to the `apartment` table (`apartment_id`, `name`, `url`, `is_active`).
2. Ingest the **latest** date folder’s `unit_daily_feed.json` into the `daily_feed` table (full snapshot for that `as_of_date`).
3. Upload a new `apartment_list.json` built from the current `apartment` table → this becomes **Muse’s source for the next run**.

Muse should assume: **`apartment_list.json` is replaced by the server after ingest**; the file Muse reads today may differ from tomorrow’s.

---

## File 1 — `apartment_list.json` (server → Muse)

**Key:** `daily_feed_json/apartment_list.json`  
**Producer:** apartment server (after daily ingest)  
**Consumer:** Muse (starting point for verify + scrape)

### JSON shape

```json
{
  "generated_at": "2026-11-01T20:00:00+00:00",
  "source": "apartment.apartment",
  "apartments": [
    {
      "apartment_id": 1,
      "name": "Livingston Terrace",
      "url": "https://example.com/...",
      "is_active": true
    }
  ]
}
```

A bare array `[{ ... }, ...]` is also accepted by the server when **reading** lists; **published** files use the wrapped object above.

### Field rules

| Field | Type | Meaning |
|-------|------|---------|
| `apartment_id` | integer | **Stable ID** assigned by the server. Muse must **never renumber** existing IDs. New buildings get new IDs only from the server. |
| `name` | string | Display / marketing name (may change over time). |
| `url` | string | **Scrape entry URL** Muse should use (availability / listings page). |
| `is_active` | boolean | If `false`, Muse should **not** scrape; row remains for history. |

One row = **one building** in our database (not a multi-building operator blob).

### Authoritative ID lookup (Muse must follow)

**`apartment_list.json` is the only reference for which building is which `apartment_id`.**

| Muse needs to… | Use |
|----------------|-----|
| Know **`apartment_id` ↔ name ↔ url`** | **`daily_feed_json/apartment_list.json`** (after verify, same IDs in `apartment_list_muse_verified.json`) |
| Decide **which URL to scrape** | `url` on that row (`is_active: true` only) |
| Fill **`apartment_id` on each unit** in `unit_daily_feed.json` | **Copy the integer `apartment_id` from the list row for the building you scraped** — do not guess from name, address, or operator slug |

Rules:

1. **Do not mint new `apartment_id` values** in unit files. IDs are allocated by the server and published in `apartment_list.json`.
2. **Do not use** legacy keys such as `property_id`, `property`, operator bundle names, or Rockrose slugs in `unit_daily_feed.json` — map scrape targets back to a row in **`apartment_list.json`** and use its `apartment_id`.
3. If a scraped site does not clearly match any row, **do not** assign a random ID; flag it or omit units until the server adds a row to the next `apartment_list.json`.
4. When **`name` or `url` changes**, keep the same **`apartment_id`**; update catalog via `apartment_list_muse_verified.json`, not by inventing a new ID in unit output.

Example: if the list contains `"apartment_id": 42, "name": "4720 Center Boulevard", "url": "https://…"`, every unit scraped from that building’s listing page must have `"apartment_id": 42` in `unit_daily_feed.json`.

---

## File 2 — `apartment_list_muse_verified.json` (Muse → server)

**Key:** `daily_feed_json/apartment_list_muse_verified.json`  
**Producer:** Muse (daily, after verify)  
**Consumer:** server (step 1 of daily job)

Same schema as `apartment_list.json` (`apartments[]` with `apartment_id`, `name`, `url`, `is_active`).

### What Muse should update

- Correct **`url`** after redirects, operator changes, or bad links in the previous list.
- Correct **`name`** if the site rebrands (same `apartment_id`).
- Set **`is_active`** to `false` for buildings that should no longer be scraped (do **not** remove IDs the server still knows about).

The server **upserts by `apartment_id`**: existing rows get `name` / `url` / `is_active` updated; unknown IDs may be inserted if Muse adds rows the server did not publish (discouraged unless coordinated).

If this file is **missing** on a given run, the server skips catalog sync and only ingests units + republishes `apartment_list.json`.

---

## File 3 — `unit_daily_feed.json` (Muse → server)

**Key:** `daily_feed_json/{YYYY-MM-DD}/unit_daily_feed.json`  
**Example:** `daily_feed_json/2026-11-01/unit_daily_feed.json`  
**Producer:** Muse (daily scrape)  
**Consumer:** server (step 2 of daily job)

`{YYYY-MM-DD}` must match the **snapshot date** (`as_of_date`) for that scrape.

### Schema (normative)

**Root object** (preferred):

| Property | JSON type | Required | Description |
|----------|-----------|----------|-------------|
| `as_of_date` | string | **Yes** | ISO calendar date `YYYY-MM-DD`. Must equal the folder name `{YYYY-MM-DD}`. |
| `generated_at` | string | No | ISO 8601 timestamp when Muse finished the file (UTC recommended). |
| `units` | array | **Yes** | Full on-market snapshot; may be empty `[]` if nothing scraped. |

**Each element of `units[]`:**

| Property | JSON type | Required | Maps to DB column | Description |
|----------|-----------|----------|-------------------|-------------|
| `apartment_id` | integer | **Yes** | `daily_feed.apartment_id` | **Must equal** the `apartment_id` from the matching row in **`apartment_list.json`** (same ID in verified list). Server rejects unknown IDs. |
| `unit_number` | string | **Yes** | `daily_feed.unit_number` | Unit label as on site (e.g. `"#1804"`). Max 32 chars; keep consistent day-to-day. |
| `floorplan` | string \| null | No | `daily_feed.floorplan` | Floor plan / unit type name. |
| `bedrooms` | number \| null | No | `daily_feed.bedrooms` | Studio = `0`. Fractions allowed (e.g. `0`, `1`, `2`). |
| `bathrooms` | number \| null | No | `daily_feed.bathrooms` | e.g. `1`, `1.5`, `2`. |
| `size_sqft` | integer \| null | No | `daily_feed.size_sqft` | Interior area. Omit or `null` if unknown — **do not** send sentinel values like `1`. |
| `rent_min` | number \| null | No | `daily_feed.rent_min` | Listed low price (USD). |
| `rent_max` | number \| null | No | `daily_feed.rent_max` | Listed high price; equals `rent_min` when single price. |
| `availability` | string \| null | No | `daily_feed.availability` | Raw availability text from site (`"Now"`, `"11/14/2026"`, etc.). |
| `move_in_date` | string \| null | No | `daily_feed.move_in_date` | ISO date `YYYY-MM-DD` when Muse can normalize; else omit and server may derive from `availability`. |
| `months_free` | number \| null | No | `daily_feed.months_free` | Concession months (e.g. `1`, `1.5`) when applicable. |
| `op` | string \| null | No | `daily_feed.op` | OP / broker concession text (e.g. `"1 Month OP"`). |
| `discount` | string \| null | No | `daily_feed.discount` | Other promo summary. |

**Row identity (server):** primary key `(apartment_id, unit_number, as_of_date)` where `as_of_date` comes from the **root** `as_of_date`, not repeated per unit.

**Do not include** in unit objects (already on `apartment` or redundant): `property`, `property_id`, `building`, `name`, `address`, `zip`, `url`, `scraped_date`, `snapshot_date`.

**Alternate root (discouraged):** a JSON **array** of unit objects, each **must** include `"as_of_date": "YYYY-MM-DD"` on every row, and all rows must share the same date. The server prefers the wrapped object above.

### JSON Schema (draft 2020-12)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://apartment.local/schemas/unit_daily_feed.json",
  "title": "unit_daily_feed",
  "type": "object",
  "required": ["as_of_date", "units"],
  "additionalProperties": false,
  "properties": {
    "as_of_date": {
      "type": "string",
      "format": "date",
      "pattern": "^\\d{4}-\\d{2}-\\d{2}$"
    },
    "generated_at": { "type": "string", "format": "date-time" },
    "units": {
      "type": "array",
      "items": { "$ref": "#/$defs/unit" }
    }
  },
  "$defs": {
    "unit": {
      "type": "object",
      "required": ["apartment_id", "unit_number"],
      "additionalProperties": false,
      "properties": {
        "apartment_id": { "type": "integer", "minimum": 1 },
        "unit_number": { "type": "string", "minLength": 1, "maxLength": 32 },
        "floorplan": { "type": ["string", "null"] },
        "bedrooms": { "type": ["number", "null"] },
        "bathrooms": { "type": ["number", "null"] },
        "size_sqft": { "type": ["integer", "null"], "minimum": 2 },
        "rent_min": { "type": ["number", "null"], "minimum": 0 },
        "rent_max": { "type": ["number", "null"], "minimum": 0 },
        "availability": { "type": ["string", "null"] },
        "move_in_date": {
          "type": ["string", "null"],
          "format": "date",
          "pattern": "^\\d{4}-\\d{2}-\\d{2}$"
        },
        "months_free": { "type": ["number", "null"], "minimum": 0 },
        "op": { "type": ["string", "null"] },
        "discount": { "type": ["string", "null"] }
      }
    }
  }
}
```

### Example file

```json
{
  "as_of_date": "2026-11-01",
  "generated_at": "2026-11-01T14:00:00+00:00",
  "units": [
    {
      "apartment_id": 2,
      "unit_number": "#1804",
      "floorplan": "Residence 04",
      "bedrooms": 1,
      "bathrooms": 1,
      "size_sqft": 537,
      "rent_min": 2794.0,
      "rent_max": 3055.0,
      "availability": "Now",
      "move_in_date": "2026-11-01",
      "months_free": null,
      "op": null,
      "discount": null
    },
    {
      "apartment_id": 42,
      "unit_number": "#1712",
      "floorplan": "1 Bed",
      "bedrooms": 1,
      "bathrooms": 1,
      "size_sqft": null,
      "rent_min": 4575.0,
      "rent_max": 4575.0,
      "availability": "2026-10-22",
      "move_in_date": "2026-10-22",
      "months_free": 1.0,
      "op": null,
      "discount": null
    }
  ]
}
```

### Validation rules (server)

- Reject or skip units with unknown `apartment_id` (not in `apartment` table).
- Reject file if root `as_of_date` ≠ folder date.
- Duplicate `(apartment_id, unit_number)` in the same file → last-wins or error (TBD; avoid duplicates in Muse output).

### Snapshot semantics

- One file = **full on-market snapshot for that `as_of_date`** (all active buildings Muse scraped that day).
- Server replaces all `daily_feed` rows for that date on ingest (delete by `as_of_date`, then insert).
- Units **not** in the file for that date are treated as **not on market** that day (for that ingest).

---

## Timing & coordination

| Step | Who | Action |
|------|-----|--------|
| T0 | Server | Publishes `apartment_list.json` (from DB). |
| T1 | Muse | Reads `apartment_list.json`, verifies URLs, writes `apartment_list_muse_verified.json`. |
| T2 | Muse | Scrapes using verified list (+ URLs), writes `daily_feed_json/{date}/unit_daily_feed.json`. |
| T3 | Server | Runs daily job: apply verified list → ingest `unit_daily_feed.json` → upload new `apartment_list.json`. |

Exact cron times are up to us; Muse should finish **verified list + unit file** before the server job runs.

---

## IDs and building granularity

- **`apartment_id`** is the join key everywhere (catalog + unit feed + database).
- **Which name/url maps to which ID:** always from **`apartment_list.json`** (see [Authoritative ID lookup](#authoritative-id-lookup-muse-must-follow)).
- One **`apartment_id`** = one physical building (or single scrape target). Multi-building operators are split into multiple IDs on the server side.
- Muse must **preserve `apartment_id`** across renames and URL changes (same ID in verified list + unit feed).

---

## Legacy / transition notes

Historically the bucket also contained per-operator files such as `merged_{date}.json` and `units_{date}.json` under date folders. **Going forward, the contract files are:**

1. `daily_feed_json/apartment_list.json`
2. `daily_feed_json/apartment_list_muse_verified.json`
3. `daily_feed_json/{YYYY-MM-DD}/unit_daily_feed.json`

Please align new Muse output with **`apartment_id`** in unit rows (not `property_id` slug).

---

## Server entrypoint (reference)

```bash
python scripts/daily_sync.py
python scripts/daily_sync.py --date 2026-11-02   # optional: specific snapshot date
```

Steps: apply `apartment_list_muse_verified.json` if present → ingest latest (or `--date`) `{YYYY-MM-DD}/unit_daily_feed.json` → upload fresh `apartment_list.json`.

After unit ingest, each building in the file gets `apartment.last_feed_date` / `last_feed_unit_count`. The script JSON includes `active_missing_today`: active buildings **not** in that day's feed (for Muse/debug).

DB columns (if missing, ingest auto-adds): run `scripts/add_apartment_last_feed_date.sql` once in older environments.

---

## Questions

Contact Gavin for bucket IAM, new `apartment_id` allocation, or schema changes.
