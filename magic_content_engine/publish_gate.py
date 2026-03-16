"""Publish Gate review for generated content outputs.

After bundle assembly, each Content_Output is presented to the user
with filename, word count, and first 3 lines. The user chooses:
  [1] Approve  — add to S3 upload list
  [2] Skip     — keep locally, log "skipped at publish gate"
  [3] Hold     — prompt for release date, move to held directory
  [4] Review   — move to review directory for manual inspection

In unattended mode all outputs are saved locally with no auto-approve,
and the caller is expected to notify via SES.

Requirements: REQ-030.1–REQ-030.8
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Callable, Optional, Protocol

from magic_content_engine.config import HELD_OUTPUT_PATH, REVIEW_OUTPUT_PATH
from magic_content_engine.models import HeldItem, ReviewItem

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enum & result dataclass
# ---------------------------------------------------------------------------


class PublishGateDecision(Enum):
    """User decision at the Publish Gate."""

    APPROVE = "approve"
    SKIP = "skip"
    HOLD = "hold"
    REVIEW = "review"


@dataclass
class PublishGateResult:
    """Outcome of the Publish Gate review for a single Content_Output."""

    filename: str
    decision: PublishGateDecision
    release_date: Optional[date] = None
    held_item: Optional[HeldItem] = None
    review_item: Optional[ReviewItem] = None


# ---------------------------------------------------------------------------
# File-operation protocol (testable seam)
# ---------------------------------------------------------------------------


class FileOps(Protocol):
    """Protocol for file move operations so callers can inject fakes."""

    def move_file(self, src: str, dest_dir: str, filename: str) -> str:
        """Move *src* into *dest_dir*/*filename*. Return the final path."""
        ...


class DefaultFileOps:
    """Production implementation that uses :func:`shutil.move`."""

    def move_file(self, src: str, dest_dir: str, filename: str) -> str:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, filename)
        shutil.move(src, dest)
        return dest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def format_output_preview(filename: str, content: str) -> str:
    """Return a preview string: filename, word count, first 3 lines."""
    word_count = len(content.split())
    lines = content.splitlines()
    first_three = "\n".join(lines[:3])
    return (
        f"  File: {filename}\n"
        f"  Words: {word_count}\n"
        f"  Preview:\n{first_three}"
    )


def _parse_date(raw: str) -> Optional[date]:
    """Try to parse *raw* as YYYY-MM-DD. Return ``None`` on failure."""
    raw = raw.strip()
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Single-output interactive prompt
# ---------------------------------------------------------------------------


def prompt_publish_gate(
    filename: str,
    content: str,
    slug: str,
    run_date: date,
    *,
    bundle_dir: str = "",
    s3_key_prefix: str = "",
    article_titles: Optional[list[str]] = None,
    file_ops: FileOps | None = None,
    input_fn: Callable[[str], str] = input,
) -> PublishGateResult:
    """Run the Publish Gate prompt for a single Content_Output.

    Parameters
    ----------
    filename:
        Name of the generated file (e.g. ``"post.md"``).
    content:
        Full text content of the file.
    slug:
        Run slug (e.g. ``"agentcore-browser-launch"``).
    run_date:
        Date of the current run.
    bundle_dir:
        Local path to the output bundle directory containing *filename*.
    s3_key_prefix:
        S3 key prefix for this run (e.g. ``"output/2025-07-14-slug/"``).
    article_titles:
        Titles of articles covered in this output (used for HeldItem).
    file_ops:
        Injectable file-operation handler. Defaults to :class:`DefaultFileOps`.
    input_fn:
        Callable for reading user input. Override in tests.

    Returns
    -------
    PublishGateResult
    """
    if file_ops is None:
        file_ops = DefaultFileOps()
    if article_titles is None:
        article_titles = []

    date_str = run_date.isoformat()
    dir_name = f"{date_str}-{slug}"

    print(f"\n--- Publish Gate ---\n{format_output_preview(filename, content)}")
    print("\n  [1] Approve   [2] Skip   [3] Hold (+ date)   [4] Review")

    while True:
        choice = input_fn("Decision (1-4): ").strip()

        # --- Approve ---
        if choice == "1":
            logger.info("Publish gate: APPROVED %s", filename)
            return PublishGateResult(filename=filename, decision=PublishGateDecision.APPROVE)

        # --- Skip ---
        if choice == "2":
            logger.info("Publish gate: skipped at publish gate — %s", filename)
            return PublishGateResult(filename=filename, decision=PublishGateDecision.SKIP)

        # --- Hold ---
        if choice == "3":
            release = _prompt_release_date(input_fn)
            held_dir = os.path.join(HELD_OUTPUT_PATH, dir_name)
            src_path = os.path.join(bundle_dir, filename) if bundle_dir else filename
            local_path = file_ops.move_file(src_path, held_dir, filename)

            held_item = HeldItem(
                filename=filename,
                s3_destination_path=f"{s3_key_prefix}{filename}",
                release_date=release,
                article_titles=list(article_titles),
                run_date=run_date,
                local_file_path=local_path,
            )
            logger.info(
                "Publish gate: HELD %s until %s at %s",
                filename,
                release.isoformat(),
                local_path,
            )
            return PublishGateResult(
                filename=filename,
                decision=PublishGateDecision.HOLD,
                release_date=release,
                held_item=held_item,
            )

        # --- Review ---
        if choice == "4":
            review_dir = os.path.join(REVIEW_OUTPUT_PATH, dir_name)
            src_path = os.path.join(bundle_dir, filename) if bundle_dir else filename
            local_path = file_ops.move_file(src_path, review_dir, filename)

            review_item = ReviewItem(
                filename=filename,
                run_date=run_date,
                local_file_path=local_path,
                reason="held for manual review",
            )
            logger.info("Publish gate: held for manual review — %s at %s", filename, local_path)
            return PublishGateResult(
                filename=filename,
                decision=PublishGateDecision.REVIEW,
                review_item=review_item,
            )

        print("Invalid choice. Enter 1, 2, 3, or 4.")


def _prompt_release_date(input_fn: Callable[[str], str]) -> date:
    """Keep asking until the user provides a valid YYYY-MM-DD date."""
    while True:
        raw = input_fn("Release date (YYYY-MM-DD): ")
        parsed = _parse_date(raw)
        if parsed is not None:
            return parsed
        print("Invalid date format. Please use YYYY-MM-DD.")


# ---------------------------------------------------------------------------
# Run gate for all outputs
# ---------------------------------------------------------------------------


def run_publish_gate(
    outputs: dict[str, str],
    slug: str,
    run_date: date,
    unattended: bool,
    *,
    bundle_dir: str = "",
    s3_key_prefix: str = "",
    article_titles: Optional[list[str]] = None,
    file_ops: FileOps | None = None,
    input_fn: Callable[[str], str] = input,
) -> list[PublishGateResult]:
    """Run the Publish Gate for every output in *outputs*.

    Parameters
    ----------
    outputs:
        Mapping of ``{filename: content}`` for each generated
        Content_Output.
    slug:
        Run slug.
    run_date:
        Date of the current run.
    unattended:
        When ``True``, save all outputs locally without prompting.
        No auto-approve. The caller should notify via SES.
    bundle_dir:
        Local path to the output bundle directory.
    s3_key_prefix:
        S3 key prefix for this run.
    article_titles:
        Titles of articles covered (passed through to HeldItem).
    file_ops:
        Injectable file-operation handler.
    input_fn:
        Callable for reading user input.

    Returns
    -------
    list[PublishGateResult]
        One result per output, in iteration order.
    """
    if article_titles is None:
        article_titles = []

    results: list[PublishGateResult] = []

    if unattended:
        logger.info(
            "Unattended mode: saving all %d outputs locally, no auto-approve.",
            len(outputs),
        )
        for fname in outputs:
            logger.info("Publish gate (unattended): skipped at publish gate — %s", fname)
            results.append(
                PublishGateResult(filename=fname, decision=PublishGateDecision.SKIP)
            )
        return results

    for fname, content in outputs.items():
        result = prompt_publish_gate(
            filename=fname,
            content=content,
            slug=slug,
            run_date=run_date,
            bundle_dir=bundle_dir,
            s3_key_prefix=s3_key_prefix,
            article_titles=article_titles,
            file_ops=file_ops,
            input_fn=input_fn,
        )
        results.append(result)

    return results
