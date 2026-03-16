"""S3 upload with retry for Publish_Gate-approved files.

Uploads only approved files to a configured S3 bucket under
``output/YYYY-MM-DD-[slug]/``. Retries up to 3 times with
exponential backoff (1s, 2s, 4s). Logs failure after exhausting
retries. Skips upload entirely when no files are approved.

Requirements: REQ-017.5, REQ-024.1, REQ-024.2
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Protocol

from magic_content_engine.errors import ErrorCollector, StepError

logger = logging.getLogger(__name__)

_BACKOFF_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
_MAX_ATTEMPTS: int = 3


# ---------------------------------------------------------------------------
# S3 client protocol — testable seam
# ---------------------------------------------------------------------------


class S3ClientProtocol(Protocol):
    """Protocol for S3 upload operations.

    Any object matching this interface can be injected, making the
    upload logic testable without touching real AWS infrastructure.
    """

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        """Upload a local file to S3.

        Raises on failure so the retry wrapper can catch and retry.
        """
        ...


# ---------------------------------------------------------------------------
# Upload with retry
# ---------------------------------------------------------------------------


def upload_approved_files(
    client: S3ClientProtocol,
    approved_files: list[str],
    bucket: str,
    s3_key_prefix: str,
    collector: ErrorCollector,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Upload Publish_Gate-approved files to S3 with retry.

    Parameters
    ----------
    client:
        An object satisfying :class:`S3ClientProtocol`.
    approved_files:
        Local file paths approved at the Publish Gate.
    bucket:
        Target S3 bucket name.
    s3_key_prefix:
        Key prefix, e.g. ``output/2025-07-14-agentcore-browser-launch/``.
    collector:
        Error collector for logging failures.
    sleep_fn:
        Injected sleep function (default ``time.sleep``). Tests can
        pass a no-op to avoid real delays.

    Returns
    -------
    list[str]
        S3 keys of successfully uploaded files.
    """
    if not approved_files:
        logger.info("No approved files — skipping S3 upload.")
        return []

    uploaded_keys: list[str] = []

    for local_path in approved_files:
        # Derive the S3 key from the prefix + filename
        filename = local_path.rsplit("/", 1)[-1] if "/" in local_path else local_path
        s3_key = f"{s3_key_prefix}{filename}"

        if _upload_single_file(client, local_path, bucket, s3_key, collector, sleep_fn):
            uploaded_keys.append(s3_key)

    return uploaded_keys


def _upload_single_file(
    client: S3ClientProtocol,
    local_path: str,
    bucket: str,
    s3_key: str,
    collector: ErrorCollector,
    sleep_fn: Callable[[float], None],
) -> bool:
    """Attempt to upload a single file with exponential backoff.

    Returns ``True`` on success, ``False`` after exhausting retries.
    """
    last_exc: Exception | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            client.upload_file(local_path, bucket, s3_key)
            logger.info("Uploaded %s → s3://%s/%s", local_path, bucket, s3_key)
            return True
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS:
                delay = _BACKOFF_DELAYS[attempt - 1]
                logger.warning(
                    "S3 upload retry %d/%d for %s — waiting %.1fs: %s",
                    attempt,
                    _MAX_ATTEMPTS,
                    local_path,
                    delay,
                    exc,
                )
                sleep_fn(delay)

    # All attempts exhausted — log via collector and continue
    assert last_exc is not None
    collector.add(
        StepError(
            step="upload",
            target=local_path,
            error_message=str(last_exc),
            context={"retry_count": _MAX_ATTEMPTS, "bucket": bucket, "s3_key": s3_key},
        )
    )
    return False
