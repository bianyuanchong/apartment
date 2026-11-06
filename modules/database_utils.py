from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extensions import connection as PgConnection
from psycopg2.extensions import cursor as PgCursor

REPO_ROOT = Path(__file__).resolve().parent.parent
CREDS_PATH = REPO_ROOT / "creds" / "database.json"


def _load_creds(profile: str = "localhost") -> dict[str, Any]:
    data = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
    if profile not in data:
        raise KeyError(f"Unknown database profile {profile!r} in {CREDS_PATH}")
    return data[profile]


def db_connect(profile: str = "localhost") -> tuple[PgConnection, PgCursor]:
    """Open a PostgreSQL connection and cursor using creds/database.json."""
    conn = psycopg2.connect(**_load_creds(profile))
    cursor = conn.cursor()
    return conn, cursor
