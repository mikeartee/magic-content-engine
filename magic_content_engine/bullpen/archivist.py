"""Whakaaro — the Archivist Lambda.

Reads vault notes from ``s3://mce-second-brain/ami-context/``, produces a
structured summary of entries modified in the last 7 days, and writes the
result to ``s3://mce-second-brain/archive/YYYY-MM-DD-summary.json``.

Exits cleanly (returns ``items_archived=0``) when the feed is empty or S3 is
unreachable — no exception is raised in either case.

IAM constraints (enforced at the execution role level):
  - S3 GetObject  — scoped to ``mce-second-brain/ami-context/``
  - S3 PutObject  — scoped to ``mce-second-brain/archive/``
  - CloudWatch Logs

Requirements: REQ-17, REQ-18
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from magic_content_engine.config import (
    MCE_S3_AMI_CONTEXT_PREFIX,
    MCE_S3_ARCHIVE_PREFIX,
    MCE_SECOND_BRAIN_BUCKET,
)

logger = logging.getLogger(__name__)

# How far back to look for recent vault entries.
_LOOKBACK_DAYS: int = 7


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(
    run_date: str | None = None,
    s3_client: Any = None,
    bucket: str = MCE_SECOND_BRAIN_BUCKET,
    context_prefix: str = MCE_S3_AMI_CONTEXT_PREFIX,
    archive_prefix: str = MCE_S3_ARCHIVE_PREFIX,
) -> dict[str, Any]:
    """Run the Archivist for the given date.

    Parameters
    ----------
    run_date:
        ISO 8601 date string (``YYYY-MM-DD``).  Defaults to today (UTC).
        Passed explicitly in tests for deterministic behaviour.
    s3_client:
        Injected boto3 S3 client.  When ``None`` a real client is created.
        Inject a fake in tests to avoid touching AWS.
    bucket:
        S3 bucket name.  Defaults to ``MCE_SECOND_BRAIN_BUCKET``.
    context_prefix:
        S3 key prefix for the nightly context feed.
    archive_prefix:
        S3 key prefix for archive output.

    Returns
    -------
    dict
        ``{"items_archived": int, "run_timestamp": str}``
    """
    effective_date: date = (
        date.fromisoformat(run_date) if run_date else date.today()
    )
    run_timestamp: str = datetime.now(tz=timezone.utc).isoformat()

    logger.info("Whakaaro starting — run_date=%s", effective_date)

    if s3_client is None:
        s3_client = boto3.client("s3")

    # --- List objects in ami-context/ ---
    try:
        objects = _list_context_objects(s3_client, bucket, context_prefix)
    except Exception as exc:
        logger.warning(
            "Whakaaro: S3 unreachable or listing failed — %s. "
            "Returning items_archived=0.",
            exc,
        )
        return {"items_archived": 0, "run_timestamp": run_timestamp}

    if not objects:
        logger.info(
            "Whakaaro: ami-context/ is empty — nothing to archive. "
            "Returning items_archived=0."
        )
        return {"items_archived": 0, "run_timestamp": run_timestamp}

    # --- Filter to last 7 days ---
    cutoff: datetime = datetime.combine(
        effective_date, datetime.min.time(), tzinfo=timezone.utc
    ) - timedelta(days=_LOOKBACK_DAYS - 1)

    recent = [obj for obj in objects if obj["LastModified"] >= cutoff]

    if not recent:
        logger.info(
            "Whakaaro: no entries modified in the last %d days — "
            "returning items_archived=0.",
            _LOOKBACK_DAYS,
        )
        return {"items_archived": 0, "run_timestamp": run_timestamp}

    # --- Read each recent object ---
    entries: list[dict[str, Any]] = []
    for obj in recent:
        key: str = obj["Key"]
        try:
            content = _get_object_body(s3_client, bucket, key)
        except Exception as exc:
            logger.warning("Whakaaro: failed to read s3://%s/%s — %s", bucket, key, exc)
            continue

        entries.append(
            {
                "key": key,
                "last_modified": obj["LastModified"].isoformat(),
                "size_bytes": obj["Size"],
                "content": content,
            }
        )

    # --- Build summary ---
    summary: dict[str, Any] = {
        "run_date": effective_date.isoformat(),
        "run_timestamp": run_timestamp,
        "lookback_days": _LOOKBACK_DAYS,
        "items_archived": len(entries),
        "entries": entries,
    }

    # --- Write to archive/ ---
    archive_key = f"{archive_prefix}{effective_date.isoformat()}-summary.json"
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=archive_key,
            Body=json.dumps(summary, ensure_ascii=False, indent=2),
            ContentType="application/json",
        )
        logger.info(
            "Whakaaro: wrote %d entries to s3://%s/%s",
            len(entries),
            bucket,
            archive_key,
        )
    except Exception as exc:
        logger.warning(
            "Whakaaro: failed to write archive to s3://%s/%s — %s. "
            "Returning items_archived=0.",
            bucket,
            archive_key,
            exc,
        )
        return {"items_archived": 0, "run_timestamp": run_timestamp}

    return {"items_archived": len(entries), "run_timestamp": run_timestamp}


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point.

    Accepts an optional ``run_date`` field in the event payload for
    testability.  EventBridge Scheduler invocations will not include it,
    so it defaults to today.
    """
    run_date: str | None = event.get("run_date")
    return run(run_date=run_date)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _list_context_objects(
    s3_client: Any,
    bucket: str,
    prefix: str,
) -> list[dict[str, Any]]:
    """Return all objects under *prefix* using paginated ListObjectsV2.

    Returns an empty list when the prefix contains no objects.
    Raises on S3 errors so the caller can handle them uniformly.
    """
    objects: list[dict[str, Any]] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
    for page in pages:
        for obj in page.get("Contents", []):
            # Skip the prefix "directory" placeholder itself
            if obj["Key"] == prefix:
                continue
            objects.append(obj)
    return objects


def _get_object_body(s3_client: Any, bucket: str, key: str) -> str:
    """Fetch and decode the body of an S3 object as UTF-8 text."""
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read().decode("utf-8", errors="replace")
