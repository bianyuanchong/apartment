"""
Cross-check catalog, S3 feeds, and apartment.daily_feed after ingest.

  python scripts/verify_data_integrity.py
  python scripts/verify_data_integrity.py --date 2026-11-05
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from modules.apartment_list_sync import (  # noqa: E402
    OUTPUT_FILENAME,
    VERIFIED_FILENAME,
    _normalize_rows,
    fetch_apartments_from_db,
)
from modules.aws_utils import s3_get_json, s3_key  # noqa: E402
from modules.database_utils import db_connect  # noqa: E402
from modules.building_apartment_map import load_apartment_names  # noqa: E402
from modules.unit_daily_feed_ingest import (  # noqa: E402
    list_unit_daily_feed_dates,
    load_valid_apartment_ids,
    rows_from_units,
    _parse_payload,
    unit_daily_feed_s3_key,
)


def load_csv_names(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    names: list[str] = []
    for row in reader:
        n = (row.get("name") or row.get("building") or "").strip()
        if n:
            names.append(n)
    return names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="", help="Latest feed date to deep-check (default: max on S3)")
    args = parser.parse_args()

    issues: list[str] = []
    ok: list[str] = []
    notes: list[str] = []

    conn, cur = db_connect()
    try:
        db_rows = fetch_apartments_from_db(cur)
        db_by_id = {r["apartment_id"]: r for r in db_rows}
        db_names = {r["name"] for r in db_rows}

        # --- Catalog counts ---
        cur.execute("SELECT COUNT(*) FROM apartment.apartment")
        n_apartment = cur.fetchone()[0]
        if n_apartment != len(db_rows):
            issues.append(f"apartment row count mismatch: COUNT(*)={n_apartment} vs fetch={len(db_rows)}")
        else:
            ok.append(f"apartment.apartment: {n_apartment} rows")

        # Duplicate names
        cur.execute(
            """
            SELECT name, COUNT(*) FROM apartment.apartment
            GROUP BY name HAVING COUNT(*) > 1
            ORDER BY COUNT(*) DESC, name
            LIMIT 20
            """
        )
        dup_names = cur.fetchall()
        if dup_names:
            issues.append(
                f"duplicate apartment names in DB ({len(dup_names)} shown): "
                + ", ".join(f"{n!r} x{c}" for n, c in dup_names[:5])
            )
        else:
            ok.append("no duplicate apartment names in DB")

        # S3 published list vs DB
        try:
            pub = s3_get_json(s3_key(OUTPUT_FILENAME))
            pub_rows = _normalize_rows(pub)
            pub_ids = {r["apartment_id"] for r in pub_rows}
            db_ids = set(db_by_id)
            if pub_ids != db_ids:
                only_pub = sorted(pub_ids - db_ids)[:10]
                only_db = sorted(db_ids - pub_ids)[:10]
                issues.append(
                    f"S3 {OUTPUT_FILENAME} ID set != DB "
                    f"(only S3: {only_pub}, only DB: {only_db})"
                )
            elif len(pub_rows) != n_apartment:
                issues.append(f"S3 list count {len(pub_rows)} != DB {n_apartment}")
            else:
                mism = []
                for r in pub_rows:
                    d = db_by_id[r["apartment_id"]]
                    if d["name"] != r["name"] or d["url"] != r["url"] or d["is_active"] != r["is_active"]:
                        mism.append(r["apartment_id"])
                if mism:
                    issues.append(f"S3 list field mismatch for IDs: {mism[:10]}")
                else:
                    ok.append(f"S3 {OUTPUT_FILENAME} matches DB ({len(pub_rows)} apartments)")

            verified = s3_get_json(s3_key(VERIFIED_FILENAME))
            ver_rows = _normalize_rows(verified)
            ver_ids = {r["apartment_id"] for r in ver_rows}
            missing_in_db = sorted(ver_ids - db_ids)
            if missing_in_db:
                issues.append(f"verified IDs not in DB: {missing_in_db[:15]}")
            extra_not_verified = len(db_ids - ver_ids)
            ok.append(
                f"S3 verified: {len(ver_rows)} rows; DB has {extra_not_verified} IDs not in verified (expected after local catalog import)"
            )
        except Exception as exc:
            issues.append(f"S3 catalog check failed: {exc}")

        # CSV coverage (by name)
        nyc_csv = REPO_ROOT / "data" / "nyc_apartments.csv"
        jersey_csv = REPO_ROOT / "data" / "jersey_city_apartment_list_delta.csv"
        for label, path in [("nyc_apartments.csv", nyc_csv), ("jersey delta", jersey_csv)]:
            if not path.is_file():
                issues.append(f"missing {path}")
                continue
            names = load_csv_names(path)
            missing = [n for n in names if n not in db_names]
            if missing:
                issues.append(f"{label}: {len(missing)}/{len(names)} names not in DB (e.g. {missing[:3]})")
            else:
                ok.append(f"{label}: all {len(names)} names present in DB")

        # NYC CSV apartment_id column is catalog-local; DB uses portfolio/Muse IDs.
        if nyc_csv.is_file():
            text = nyc_csv.read_text(encoding="utf-8-sig")
            reader = csv.DictReader(text.splitlines())
            id_mismatch = 0
            for row in reader:
                cid = (row.get("apartment_id") or "").strip()
                name = (row.get("name") or "").strip()
                if not cid.isdigit() or not name:
                    continue
                cur.execute("SELECT apartment_id FROM apartment.apartment WHERE name = %s", (name,))
                found = cur.fetchone()
                if found and int(found[0]) != int(cid):
                    id_mismatch += 1
            if id_mismatch:
                notes.append(
                    f"nyc_apartments.csv apartment_id column != DB for {id_mismatch} rows "
                    "(expected; import matches by name, Muse uses DB ids in feeds)"
                )

        # Daily feed dates: S3 vs DB
        s3_dates = list_unit_daily_feed_dates()
        cur.execute(
            """
            SELECT as_of_date::text, COUNT(*)::int
            FROM apartment.daily_feed
            GROUP BY as_of_date
            ORDER BY as_of_date
            """
        )
        db_counts = {row[0]: row[1] for row in cur.fetchall()}

        for d in s3_dates:
            key = unit_daily_feed_s3_key(d)
            payload = s3_get_json(key)
            as_of, units = _parse_payload(payload)
            if as_of != d:
                issues.append(f"{key}: folder date {d} != payload as_of_date {as_of}")
            valid_ids = load_valid_apartment_ids(cur)
            names = load_apartment_names(cur)
            rows, stats = rows_from_units(units, as_of, valid_ids, apartment_names=names)
            expected = stats["inserted_units"]
            db_n = db_counts.get(d.isoformat())
            if db_n is None:
                issues.append(f"DB missing as_of_date {d} (S3 has {len(units)} units, ingestable {expected})")
            elif db_n != expected:
                issues.append(f"{d}: DB rows {db_n} != re-parse ingestable {expected} (file units {len(units)})")
            elif stats["skipped_unknown_apartment"] or stats["skipped_missing_unit_number"]:
                issues.append(f"{d}: skips unknown={stats['skipped_unknown_apartment']} missing_unit={stats['skipped_missing_unit_number']}")
            else:
                ok.append(f"{d}: DB {db_n} rows == S3 ingestable ({len(units)} in file)")

        s3_iso = {d.isoformat() for d in s3_dates}
        legacy = sorted(set(db_counts) - s3_iso)
        if legacy:
            notes.append(
                f"DB has {len(legacy)} as_of_date(s) not on S3 (legacy ingests): "
                + ", ".join(legacy[:5])
                + ("…" if len(legacy) > 5 else "")
            )

        check_date = date.fromisoformat(args.date) if args.date else (s3_dates[-1] if s3_dates else None)
        if check_date:
            d_str = check_date.isoformat()
            cur.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT apartment_id)
                FROM apartment.daily_feed WHERE as_of_date = %s
                """,
                (check_date,),
            )
            total, apt_count = cur.fetchone()
            cur.execute(
                """
                SELECT apartment_id, COUNT(*) FROM apartment.daily_feed
                WHERE as_of_date = %s
                GROUP BY apartment_id
                """,
                (check_date,),
            )
            feed_counts = dict(cur.fetchall())

            cur.execute(
                """
                SELECT apartment_id, last_feed_date, last_feed_unit_count
                FROM apartment.apartment
                WHERE apartment_id = ANY(%s)
                """,
                (list(feed_counts.keys()),),
            )
            meta_bad = []
            for apt_id, last_d, last_c in cur.fetchall():
                if last_d != check_date:
                    meta_bad.append((apt_id, "last_feed_date", last_d))
                if last_c != feed_counts.get(apt_id):
                    meta_bad.append((apt_id, "last_feed_unit_count", last_c))
            if meta_bad:
                issues.append(
                    f"{d_str} last_feed_* mismatch for {len(meta_bad)} fields (sample: {meta_bad[:3]})"
                )
            else:
                ok.append(
                    f"{d_str}: last_feed_date/unit_count aligned for {len(feed_counts)} buildings in feed"
                )

            # Duplicate (apartment_id, unit_number) on that date
            cur.execute(
                """
                SELECT apartment_id, unit_number, COUNT(*)
                FROM apartment.daily_feed
                WHERE as_of_date = %s
                GROUP BY apartment_id, unit_number
                HAVING COUNT(*) > 1
                LIMIT 5
                """,
                (check_date,),
            )
            dup_units = cur.fetchall()
            if dup_units:
                issues.append(f"{d_str}: duplicate unit keys in daily_feed: {dup_units}")
            else:
                ok.append(f"{d_str}: no duplicate (apartment_id, unit_number) in daily_feed")

            cur.execute(
                """
                SELECT COUNT(*) FROM apartment.daily_feed df
                LEFT JOIN apartment.apartment a ON a.apartment_id = df.apartment_id
                WHERE df.as_of_date = %s AND a.apartment_id IS NULL
                """,
                (check_date,),
            )
            orphans = cur.fetchone()[0]
            if orphans:
                issues.append(f"{d_str}: {orphans} daily_feed rows with unknown apartment_id")
            else:
                ok.append(f"{d_str}: all feed rows reference valid apartment_id")

            if check_date in s3_dates:
                key = unit_daily_feed_s3_key(check_date)
                payload = s3_get_json(key)
                as_of, units = _parse_payload(payload)
                valid_ids = load_valid_apartment_ids(cur)
                names = load_apartment_names(cur)
                s3_rows, _ = rows_from_units(
                    units, as_of, valid_ids, apartment_names=names
                )
                s3_keys = {(r[0], r[1]) for r in s3_rows}
                cur.execute(
                    """
                    SELECT apartment_id, unit_number
                    FROM apartment.daily_feed WHERE as_of_date = %s
                    """,
                    (check_date,),
                )
                db_keys = {(int(a), str(u).strip()) for a, u in cur.fetchall()}
                if s3_keys != db_keys:
                    issues.append(
                        f"{d_str}: DB (apartment_id, unit_number) set != S3 ingest "
                        f"(only S3 {len(s3_keys - db_keys)}, only DB {len(db_keys - s3_keys)})"
                    )
                else:
                    ok.append(
                        f"{d_str}: every unit row keyed by (apartment_id, unit_number) matches S3"
                    )

        # Null rent sanity on latest date
        if check_date:
            cur.execute(
                """
                SELECT COUNT(*) FROM apartment.daily_feed
                WHERE as_of_date = %s AND rent_min IS NULL AND rent_max IS NULL
                """,
                (check_date,),
            )
            no_rent = cur.fetchone()[0]
            ok.append(f"{check_date}: {no_rent}/{total} rows with both rents null (informational)")

            cur.execute(
                """
                SELECT COUNT(*) FROM apartment.apartment
                WHERE is_active = TRUE
                  AND (last_feed_date IS NULL OR last_feed_date < %s)
                """,
                (check_date,),
            )
            stale = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM apartment.apartment
                WHERE is_active = TRUE AND last_feed_date = %s
                """,
                (check_date,),
            )
            in_feed = cur.fetchone()[0]
            notes.append(
                f"On {check_date}: {in_feed} active buildings in feed; "
                f"{stale} active with no feed on that date (Muse coverage gap, not a DB error)"
            )

    finally:
        conn.close()

    report = {"ok": ok, "notes": notes, "issues": issues, "issue_count": len(issues)}
    print(json.dumps(report, indent=2))
    if issues:
        print("\nFAILED — fix issues above.", file=sys.stderr)
        return 1
    print("\nAll checks passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
