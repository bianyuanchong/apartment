"""Update apartment.apartment name/address/zip from scripts/building_catalog.json."""

from __future__ import annotations

import json
from pathlib import Path

import psycopg2

from building_catalog import load_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    creds = json.loads((REPO_ROOT / "creds" / "database.json").read_text(encoding="utf-8"))[
        "localhost"
    ]
    catalog = load_catalog()

    conn = psycopg2.connect(**creds)
    cur = conn.cursor()
    updated = 0
    for building_id, meta in catalog.items():
        cur.execute(
            """
            UPDATE apartment.apartment
            SET name = %s, address = %s, zip = %s, updated_at = NOW()
            WHERE url = %s
            """,
            (
                meta["name"],
                meta.get("address"),
                meta.get("zip"),
                f"pending:{building_id}",
            ),
        )
        updated += cur.rowcount

    conn.commit()
    cur.execute(
        "SELECT apartment_id, name, address, zip FROM apartment.apartment ORDER BY apartment_id"
    )
    for row in cur.fetchall():
        print(row)
    print(f"Updated {updated} rows (catalog has {len(catalog)} entries)")
    conn.close()


if __name__ == "__main__":
    main()
