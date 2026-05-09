"""Approval gate — SES email with signed approve/reject URLs.

Sits between the Subeditor and Publisher stages. Generates a verdict
summary email (filename, word count, first 3 lines per file), creates
HMAC-SHA256 signed tokens tied to the run_id, sends via SES, and
provides a ``check_approval`` function for token validation.

Since Lambda Durable Functions ``wait()`` is not yet a Python library
we can import, this module is a standard Python module that:
- Generates the approval email content
- Creates signed tokens for approve/reject
- Sends via SES (boto3)
- Provides ``check_approval(token, run_id)`` for token validation

Requirements: REQ-bullpen-14
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from magic_content_engine.bullpen.models import Verdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_SECRET_ENV_VAR = "APPROVAL_TOKEN_SECRET"
_DEFAULT_BASE_URL = os.getenv("APPROVAL_BASE_URL", "https://api.example.com/approval")

_DECISION_APPROVE = "approve"
_DECISION_REJECT = "reject"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class ApprovalEmailContent:
    """Content for the approval gate email.

    Attributes
    ----------
    subject:
        Email subject line.
    body_text:
        Plain-text email body with verdict summary.
    approve_url:
        Signed URL that approves publication when clicked.
    reject_url:
        Signed URL that rejects publication when clicked.
    """

    subject: str
    body_text: str
    approve_url: str
    reject_url: str


# ---------------------------------------------------------------------------
# Injectable SES client protocol
# ---------------------------------------------------------------------------


class SESClientProtocol(Protocol):
    """Protocol for SES email sending — matches publisher's interface."""

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
# Token generation and validation
# ---------------------------------------------------------------------------


def _get_secret() -> str:
    """Return the HMAC secret from the environment.

    Raises ``ValueError`` if ``APPROVAL_TOKEN_SECRET`` is not set, rather than
    silently falling back to a hardcoded string that could be forged by anyone
    who reads the source code.
    """
    secret = os.getenv(_DEFAULT_SECRET_ENV_VAR)
    if not secret:
        raise ValueError(
            f"Environment variable {_DEFAULT_SECRET_ENV_VAR!r} is not set. "
            "Set it to a strong random secret before running the pipeline."
        )
    return secret


def generate_approval_token(run_id: str, secret: str | None = None) -> str:
    """Generate a signed HMAC-SHA256 token tied to *run_id*.

    Parameters
    ----------
    run_id:
        Unique identifier for the pipeline run (e.g. ``2025-07-14-agentcore``).
    secret:
        HMAC secret. Defaults to the ``APPROVAL_TOKEN_SECRET`` env var.

    Returns
    -------
    str
        Hex-encoded HMAC-SHA256 digest of ``run_id``.
    """
    if secret is None:
        secret = _get_secret()
    return hmac.HMAC(
        secret.encode("utf-8"),
        run_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def validate_approval_token(token: str, run_id: str, secret: str | None = None) -> bool:
    """Validate a token against the expected HMAC for *run_id*.

    Uses ``hmac.compare_digest`` to prevent timing attacks.

    Parameters
    ----------
    token:
        The token to validate (hex string).
    run_id:
        The run ID the token should be tied to.
    secret:
        HMAC secret. Defaults to the ``APPROVAL_TOKEN_SECRET`` env var.

    Returns
    -------
    bool
        ``True`` if the token is valid, ``False`` otherwise.
    """
    if secret is None:
        secret = _get_secret()
    expected = generate_approval_token(run_id, secret=secret)
    return hmac.compare_digest(token, expected)


def check_approval(token: str, run_id: str, secret: str | None = None) -> bool:
    """Validate an approval token.

    Convenience alias for :func:`validate_approval_token`.

    Parameters
    ----------
    token:
        The token received from the approve/reject URL callback.
    run_id:
        The run ID the token should be tied to.
    secret:
        HMAC secret. Defaults to the ``APPROVAL_TOKEN_SECRET`` env var.

    Returns
    -------
    bool
        ``True`` if the token is valid, ``False`` otherwise.
    """
    return validate_approval_token(token, run_id, secret=secret)


# ---------------------------------------------------------------------------
# URL generation
# ---------------------------------------------------------------------------


def _build_approval_url(
    run_id: str,
    decision: str,
    token: str,
    base_url: str,
) -> str:
    """Build a signed approve or reject URL.

    Format: ``{base_url}?run_id={run_id}&decision={decision}&token={token}``
    """
    return f"{base_url}?run_id={run_id}&decision={decision}&token={token}"


# ---------------------------------------------------------------------------
# Email content generation
# ---------------------------------------------------------------------------


def format_approval_email(
    verdicts: list[Verdict],
    output_dir: str,
    run_id: str,
    base_url: str = _DEFAULT_BASE_URL,
    secret: str | None = None,
) -> ApprovalEmailContent:
    """Generate the approval gate email content.

    Includes filename, word count, and first 3 lines per publishable file.
    Spiked and escalated files are listed separately.

    Parameters
    ----------
    verdicts:
        All verdicts from the Subeditor Agent.
    output_dir:
        Local directory where content files are stored.
    run_id:
        Unique pipeline run identifier used to sign the token.
    base_url:
        Base URL for the API Gateway approval endpoint.
    secret:
        HMAC secret for token generation.

    Returns
    -------
    ApprovalEmailContent
        Dataclass with subject, body_text, approve_url, reject_url.
    """
    token = generate_approval_token(run_id, secret=secret)
    approve_url = _build_approval_url(run_id, _DECISION_APPROVE, token, base_url)
    reject_url = _build_approval_url(run_id, _DECISION_REJECT, token, base_url)

    publishable = [v for v in verdicts if v.verdict == "publish"]
    spiked = [v for v in verdicts if v.verdict == "spike"]
    escalated = [v for v in verdicts if v.verdict == "revise"]

    lines: list[str] = [
        f"Approval required — run: {run_id}",
        "=" * 60,
        "",
    ]

    if publishable:
        lines.append(f"Files ready for publication ({len(publishable)}):")
        lines.append("")
        for v in publishable:
            filepath = Path(output_dir) / v.filename
            word_count, first_lines = _read_file_preview(filepath)
            lines.append(f"  ✓ {v.filename}  ({word_count} words)")
            if first_lines:
                for line in first_lines:
                    lines.append(f"    {line}")
            lines.append("")
    else:
        lines.append("No files are ready for publication.")
        lines.append("")

    if spiked:
        lines.append(f"Spiked ({len(spiked)}):")
        for v in spiked:
            lines.append(f"  ✗ {v.filename}  — {v.feedback}")
        lines.append("")

    if escalated:
        lines.append(f"Escalated for manual review ({len(escalated)}):")
        for v in escalated:
            lines.append(f"  ⚠ {v.filename}  — {v.feedback}")
        lines.append("")

    lines += [
        "-" * 60,
        "",
        "To approve publication, click:",
        f"  {approve_url}",
        "",
        "To reject, click:",
        f"  {reject_url}",
        "",
        "This link is tied to this run only.",
    ]

    body_text = "\n".join(lines)
    subject = f"[Magic Content Engine] Approval required — {run_id}"

    return ApprovalEmailContent(
        subject=subject,
        body_text=body_text,
        approve_url=approve_url,
        reject_url=reject_url,
    )


# ---------------------------------------------------------------------------
# SES send
# ---------------------------------------------------------------------------


def send_approval_email(
    verdicts: list[Verdict],
    output_dir: str,
    run_id: str,
    ses_client: SESClientProtocol,
    sender_email: str,
    recipient_email: str,
    base_url: str = _DEFAULT_BASE_URL,
    secret: str | None = None,
) -> ApprovalEmailContent:
    """Format and send the approval gate email via SES.

    Parameters
    ----------
    verdicts:
        All verdicts from the Subeditor Agent.
    output_dir:
        Local directory where content files are stored.
    run_id:
        Unique pipeline run identifier.
    ses_client:
        An object satisfying :class:`SESClientProtocol`.
    sender_email:
        Verified SES sender address.
    recipient_email:
        Approval recipient (Mike).
    base_url:
        Base URL for the API Gateway approval endpoint.
    secret:
        HMAC secret for token generation.

    Returns
    -------
    ApprovalEmailContent
        The email content that was sent (for logging/auditing).
    """
    content = format_approval_email(
        verdicts=verdicts,
        output_dir=output_dir,
        run_id=run_id,
        base_url=base_url,
        secret=secret,
    )

    ses_client.send_email(
        sender=sender_email,
        recipient=recipient_email,
        subject=content.subject,
        body=content.body_text,
    )
    logger.info(
        "Approval email sent to %s for run %s",
        recipient_email,
        run_id,
    )

    return content


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_file_preview(filepath: Path) -> tuple[int, list[str]]:
    """Return (word_count, first_3_lines) for a file.

    If the file does not exist, returns (0, []).
    """
    if not filepath.exists():
        logger.warning("Approval gate: file not found — %s", filepath)
        return 0, []

    try:
        text = filepath.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Approval gate: could not read %s — %s", filepath, exc)
        return 0, []

    word_count = len(text.split())
    first_lines = text.splitlines()[:3]
    return word_count, first_lines
