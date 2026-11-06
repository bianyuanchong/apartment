"""Map Muse / merged building slugs to apartment.apartment_id (never guess from unit text)."""

from __future__ import annotations

import sys
from pathlib import Path

from psycopg2.extensions import cursor as PgCursor

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from building_catalog import load_catalog  # noqa: E402


def load_slug_to_apartment_id(cur: PgCursor) -> dict[str, int]:
    """
    Resolve building slug → apartment_id:
      1. pending:{slug} URLs in apartment.apartment (NYC/Jersey catalog imports)
      2. building_catalog.json slug → canonical name → unique apartment row by name
    """
    slug_map: dict[str, int] = {}

    cur.execute(
        """
        SELECT apartment_id, name
        FROM apartment.apartment
        """
    )
    by_name: dict[str, int] = {}
    for apartment_id, name in cur.fetchall():
        key = str(name).strip().lower()
        by_name[key] = int(apartment_id)

    for slug, meta in load_catalog().items():
        canon = str(meta.get("name") or "").strip().lower()
        if not canon:
            continue
        apartment_id = by_name.get(canon)
        if apartment_id is not None:
            slug_map[str(slug)] = apartment_id

    cur.execute(
        """
        SELECT apartment_id, url
        FROM apartment.apartment
        WHERE url LIKE 'pending:%%'
        """
    )
    for apartment_id, url in cur.fetchall():
        slug = str(url).removeprefix("pending:")
        slug_map[slug] = int(apartment_id)

    return slug_map


def load_apartment_names(cur: PgCursor) -> dict[int, str]:
    cur.execute("SELECT apartment_id, name FROM apartment.apartment")
    return {int(row[0]): str(row[1]).strip() for row in cur.fetchall()}
