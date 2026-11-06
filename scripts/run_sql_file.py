import json
import sys
from pathlib import Path

import psycopg2

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    sql_path = Path(sys.argv[1])
    creds = json.loads((REPO_ROOT / "creds" / "database.json").read_text(encoding="utf-8"))[
        "localhost"
    ]
    sql = sql_path.read_text(encoding="utf-8")
    conn = psycopg2.connect(**creds)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(sql)
    conn.close()
    print(f"Executed {sql_path}")


if __name__ == "__main__":
    main()
