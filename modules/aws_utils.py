from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parent.parent
AWS_CREDS_PATH = REPO_ROOT / "creds" / "aws.json"


@lru_cache
def load_aws_config() -> dict[str, Any]:
    return json.loads(AWS_CREDS_PATH.read_text(encoding="utf-8"))


def s3_client() -> BaseClient:
    cfg = load_aws_config()
    return boto3.client(
        "s3",
        region_name=cfg["region"],
        aws_access_key_id=cfg["access_key_id"],
        aws_secret_access_key=cfg["secret_access_key"],
    )


def s3_key(*parts: str) -> str:
    """Build key under configured prefix (e.g. daily_feed_json/foo.json)."""
    cfg = load_aws_config()
    prefix = cfg.get("s3_prefix", "").rstrip("/")
    rest = "/".join(p.strip("/") for p in parts if p)
    return f"{prefix}/{rest}" if prefix else rest


def s3_object_exists(key: str) -> bool:
    cfg = load_aws_config()
    try:
        s3_client().head_object(Bucket=cfg["s3_bucket"], Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def s3_get_json(key: str) -> Any:
    cfg = load_aws_config()
    resp = s3_client().get_object(Bucket=cfg["s3_bucket"], Key=key)
    return json.loads(resp["Body"].read().decode("utf-8"))


def s3_put_json(key: str, payload: Any) -> None:
    cfg = load_aws_config()
    body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    s3_client().put_object(
        Bucket=cfg["s3_bucket"],
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
