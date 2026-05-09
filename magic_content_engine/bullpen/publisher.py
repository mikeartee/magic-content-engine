"""Publisher Lambda — S3 upload and SES notification.

Receives a list of approved files (those with ``publish`` verdicts),
uploads them to ``s3://mce-second-brain/output/YYYY-MM-DD-[slug]/``,
and sends a completion notification email via SES.

S3 uploads retry up to 3 times with exponential backoff (1s, 2s, 4s).
Per-file failures are logged and skipped — remaining files continue.

Requirements: REQ-bullpen-12, REQ-bullpen-13, REQ-bullpen-25.5
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Protocol

from magic_content_engine.bullpen.models import PublicationReport, UploadedFile
from magic_content_engine.errors import ErrorCollector, StepError, log_ses_failure

logger = logging.getLogger(__name__)

_BACKOFF_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
_MAX_ATTEMPTS: int = 3


# ---------------------------------------------------------------------------
# Injectable client protocols — testable seams
# ---------------------------------------------------------------------------


class S3ClientProtocol(Protocol):
    """Protocol for S3 upload operations."""

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        """Upload a local file to S3. Raises on failure."""
        ...


class SESClientProtocol(Protocol):
    """Protocol for SES email sending."""

    def send_email(
        self,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
    ) -> None:
        """Send a plain-text email via SES. Raises on failure."""
        ...


# ---------------------------------------------------------------------------
# Core publisher function
# ---------------------------------------------------------------------------


def publish(
    approved_files: list[str],
    s3_key_prefix: str,
    bucket: str,
    s3_client: S3ClientProtocol,
    ses_client: SESClientProtocol,
    sender_email: str,
    recipient_email: str,
    collector: ErrorCollector | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> PublicationReport:
    """Upload approved files to S3 and send a completion notification.

    Parameters
    ----------
    approved_files:
        Local file paths that received a ``publish`` verdict.
    s3_key_prefix:
        S3 key prefix, e.g. ``output/2025-07-14-agentcore-launch/``.
        Must match the pattern ``output/YYYY-MM-DD-[slug]/``.
    bucket:
        Target S3 bucket name (``mce-second-brain``).
    s3_client:
        An object satisfying :class:`S3ClientProtocol`.
    ses_client:
        An object satisfying :class:`SESClientProtocol`.
    sender_email:
        Verified SES sender address.
    recipient_email:
        Notification recipient (Mike).
    collector:
        Optional error collector. A fresh one is created if not provided.
    sleep_fn:
        Injected sleep function. Tests pass a no-op to avoid real delays.

    Returns
    -------
    PublicationReport
        Structured report of what was uploaded and whether email was sent.
    """
    if collector is None:
        collector = ErrorCollector()

    run_timestamp = datetime.now(timezone.utc).isoformat()

    # --- Upload phase ---
    uploaded: list[UploadedFile] = []

    for local_path in approved_files:
        filename = _extract_filename(local_path)
        s3_key = f"{s3_key_prefix}{filename}"

        success = _upload_with_retry(
            s3_client=s3_client,
            local_path=local_path,
            bucket=bucket,
            s3_key=s3_key,
            collector=collector,
            sleep_fn=sleep_fn,
        )
        if success:
            uploaded.append(UploadedFile(local_path=local_path, s3_key=s3_key))

    # --- SES notification phase ---
    email_sent = False
    try:
        subject = f"Publication complete — {len(uploaded)} file(s) uploaded"
        body = _build_email_body(uploaded, s3_key_prefix, run_timestamp)
        ses_client.send_email(
            sender=sender_email,
            recipient=recipient_email,
            subject=subject,
            body=body,
        )
        email_sent = True
        logger.info("SES notification sent to %s", recipient_email)
    except Exception as exc:
        log_ses_failure(exc, target=recipient_email, collector=collector)
        logger.warning("SES notification failed — continuing without email: %s", exc)

    return PublicationReport(
        files_uploaded=uploaded,
        email_sent=email_sent,
        email_recipient=recipient_email,
        run_timestamp=run_timestamp,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_filename(local_path: str) -> str:
    """Return the filename component of a path (cross-platform)."""
    # Handle both forward and back slashes
    for sep in ("/", "\\"):
        if sep in local_path:
            return local_path.rsplit(sep, 1)[-1]
    return local_path


def _upload_with_retry(
    s3_client: S3ClientProtocol,
    local_path: str,
    bucket: str,
    s3_key: str,
    collector: ErrorCollector,
    sleep_fn: Callable[[float], None],
) -> bool:
    """Attempt to upload a single file with exponential backoff.

    Returns ``True`` on success, ``False`` after exhausting retries.
    Per-file failure is logged via the collector; remaining files continue.
    """
    last_exc: Exception | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            s3_client.upload_file(local_path, bucket, s3_key)
            logger.info(
                "Uploaded %s → s3://%s/%s (attempt %d)",
                local_path,
                bucket,
                s3_key,
                attempt,
            )
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

    # All attempts exhausted — log and continue with remaining files
    assert last_exc is not None
    collector.add(
        StepError(
            step="upload",
            target=local_path,
            error_message=str(last_exc),
            context={
                "retry_count": _MAX_ATTEMPTS,
                "bucket": bucket,
                "s3_key": s3_key,
            },
        )
    )
    logger.error(
        "S3 upload failed after %d attempts for %s — skipping file",
        _MAX_ATTEMPTS,
        local_path,
    )
    return False


def _build_email_body(
    uploaded: list[UploadedFile],
    s3_key_prefix: str,
    run_timestamp: str,
) -> str:
    """Build the plain-text body for the SES completion notification."""
    lines = [
        "Publication complete.",
        f"Run timestamp: {run_timestamp}",
        f"S3 prefix: {s3_key_prefix}",
        f"Files uploaded: {len(uploaded)}",
        "",
    ]
    for uf in uploaded:
        lines.append(f"  {uf.local_path} → {uf.s3_key}")
    return "\n".join(lines)
