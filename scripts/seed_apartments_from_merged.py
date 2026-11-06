"""
Load buildings from merged_YYYY-MM-DD.json into apartment.apartment (one row per building).

Usage (from repo root):
  python scripts/seed_apartments_from_merged.py merged_2026-11-01.json

Replaces all rows in apartment.apartment (TRUNCATE ... RESTART IDENTITY).
apartment_id is DB-assigned (BIGSERIAL). url is pending:{building_id} until catalog URLs exist.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

REPO_ROOT = Path(__file__).resolve().parents[1]

# Run as script: import sibling module
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from merged_group_by_building import transform  # noqa: E402


def load_buildings(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "buildings" in data:
        return data["buildings"]
    if "properties" in data:
        return transform(data)["buildings"]
    raise ValueError(f"Unrecognized JSON shape: {path}")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/seed_apartments_from_merged.py <merged.json>", file=sys.stderr)
        sys.exit(1)

    merged_path = Path(sys.argv[1])
    if not merged_path.is_file():
        merged_path = REPO_ROOT / sys.argv[1]

    buildings = load_buildings(merged_path)

    creds_path = REPO_ROOT / "creds" / "database.json"
    creds = json.loads(creds_path.read_text(encoding="utf-8"))["localhost"]

    rows = []
    for b in buildings:
        zip_val = b.get("zip")
        if zip_val in (None, "None"):
            zip_val = None
        rows.append(
            (
                b["name"],
                f"pending:{b['id']}",
                True,
                b.get("address") or None,
                zip_val,
            )
        )

    conn = psycopg2.connect(**creds)
    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE apartment.apartment RESTART IDENTITY")
    execute_values(
        cur,
        """
        INSERT INTO apartment.apartment (
            name, url, is_active, address, zip
        ) VALUES %s
        """,
        rows,
        template="(%s, %s, %s, %s, %s)",
    )
    conn.commit()

    cur.execute(
        """
        SELECT apartment_id, name, url, zip
        FROM apartment.apartment
        ORDER BY apartment_id
        """
    )
    for row in cur.fetchall():
        print(row)
    print(f"Inserted {len(rows)} buildings from {merged_path.name}")
    conn.close()


if __name__ == "__main__":
    main()
