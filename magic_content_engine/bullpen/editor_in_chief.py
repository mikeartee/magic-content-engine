"""Editor-in-Chief — pipeline orchestration and revision loop.

The Editor-in-Chief is the top-level orchestrator for the bullpen content
pipeline. It accepts a BullpenBrief, validates it, invokes agents in a fixed
sequence, manages the revision loop, gates publication on approval, and logs
all decisions.

Since Lambda Durable Functions are not yet available as a Python library we
can import, this is implemented as a standard Python orchestrator that can be
adapted to the durable functions pattern later. The key logic — pipeline
sequencing and revision loop — is fully implemented here.

Pipeline sequence:
  Researcher → Desk Editor → Writer → Subeditor → (approval gate) → Publisher

Revision loop:
  When Subeditor returns "revise", re-invoke Writer with feedback.
  Maximum 2 revision cycles per file. On max reached, escalate to manual review.
  When Subeditor returns "spike", discard file and log rationale.

Error handling:
  - Researcher failure: log, halt pipeline
  - Desk Editor failure: log, halt pipeline
  - Writer failure per output type: log, skip that type, continue remaining
  - Subeditor failure: log, mark all pending files for manual review

Requirements: Bullpen REQ-1, REQ-2, REQ-11, REQ-14, REQ-15, REQ-16, REQ-22, REQ-25
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from magic_content_engine.bullpen.models import (
    AMILogEvent,
    BullpenBrief,
    Checkpoint,
    ContentBrief,
    ResearchBrief,
    SubeditorReview,
    Verdict,
    WriterManifest,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_REVISION_CYCLES: int = 3

VALID_OUTPUT_TYPES: frozenset[str] = frozenset(
    {
        "blog",
        "youtube",
        "cfp",
        "usergroup",
        "digest",
    }
)


# ---------------------------------------------------------------------------
# PipelineResult
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """The final result of a completed (or halted) pipeline run.

    Attributes:
        status: "success", "halted", or "error"
        files_published: Filenames that received a "publish" verdict and
            were passed to the Publisher.
        files_escalated: Filenames escalated to manual review (max revisions
            reached or Subeditor failure).
        errors: List of error dicts recorded during the run.
        run_timestamp: ISO 8601 timestamp of pipeline completion.
    """

    status: str  # "success" | "halted" | "error"
    files_published: list[str] = field(default_factory=list)
    files_escalated: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    run_timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class BullpenBriefValidationError(ValueError):
    """Raised when a BullpenBrief fails validation."""


def validate_brief(brief: BullpenBrief) -> None:
    """Validate a BullpenBrief.

    Raises:
        BullpenBriefValidationError: If the brief has an empty topic or no
            valid requested output types.
    """
    if not brief.topic or not brief.topic.strip():
        raise BullpenBriefValidationError(
            "BullpenBrief.topic must be a non-empty string"
        )

    if not brief.requested_outputs:
        raise BullpenBriefValidationError(
            "BullpenBrief.requested_outputs must contain at least one output type"
        )

    valid = [o for o in brief.requested_outputs if o in VALID_OUTPUT_TYPES]
    if not valid:
        raise BullpenBriefValidationError(
            f"BullpenBrief.requested_outputs contains no valid output types. "
            f"Got {brief.requested_outputs!r}. "
            f"Valid types: {sorted(VALID_OUTPUT_TYPES)}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_dict(data: Any) -> str:
    """Return a short SHA-256 hex digest of the JSON-serialised *data*."""
    serialised = json.dumps(data, default=str, sort_keys=True)
    return hashlib.sha256(serialised.encode()).hexdigest()[:16]


def _log(
    log_fn: Callable,
    event_type: str,
    agent_type: str,
    details: dict[str, Any],
    run_id: str = "",
) -> None:
    """Emit a structured AMILogEvent via *log_fn*."""
    event = AMILogEvent(
        event_type=event_type,
        timestamp=_now_iso(),
        agent_type=agent_type,
        run_id=run_id,
        details=details,
    )
    log_fn(event)


def _checkpoint(
    checkpoint_fn: Callable,
    agent_type: str,
    output_hash: str,
    status: str,
) -> None:
    """Save a Checkpoint via *checkpoint_fn*."""
    cp = Checkpoint(
        agent_type=agent_type,
        completion_timestamp=_now_iso(),
        output_hash=output_hash,
        status=status,
    )
    checkpoint_fn(cp)


# ---------------------------------------------------------------------------
# Revision loop helpers
# ---------------------------------------------------------------------------


def _collect_revise_filenames(review: SubeditorReview) -> list[str]:
    return [v.filename for v in review.verdicts if v.verdict == "revise"]


def _collect_publish_filenames(review: SubeditorReview) -> list[str]:
    return [v.filename for v in review.verdicts if v.verdict == "publish"]


def _collect_spike_verdicts(review: SubeditorReview) -> list[Verdict]:
    return [v for v in review.verdicts if v.verdict == "spike"]


def _build_revision_feedback(review: SubeditorReview) -> str:
    """Concatenate all revise feedback into a single string for the Writer."""
    parts = []
    for v in review.verdicts:
        if v.verdict == "revise":
            parts.append(f"File: {v.filename}\nFeedback: {v.feedback}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    brief: BullpenBrief,
    researcher_fn: Callable,
    desk_editor_fn: Callable,
    writer_fn: Callable,
    subeditor_fn: Callable,
    publisher_fn: Callable,
    approval_fn: Callable,
    log_fn: Callable,
    checkpoint_fn: Callable,
) -> PipelineResult:
    """Run the full bullpen content pipeline.

    Parameters
    ----------
    brief:
        The BullpenBrief initiating this content run.
    researcher_fn:
        Callable(brief: BullpenBrief) -> ResearchBrief
    desk_editor_fn:
        Callable(research_brief: ResearchBrief, topic: str, output_types: list[str]) -> ContentBrief
    writer_fn:
        Callable(content_brief: ContentBrief, revision_feedback: str | None) -> WriterManifest
    subeditor_fn:
        Callable(manifest: WriterManifest) -> SubeditorReview
    publisher_fn:
        Callable(approved_files: list[str]) -> Any
    approval_fn:
        Callable(review: SubeditorReview) -> bool
        Presents the approval gate and returns True if approved.
    log_fn:
        Callable(event: AMILogEvent) -> None
        Logs an AMILogEvent to DynamoDB mce-run-history.
    checkpoint_fn:
        Callable(checkpoint: Checkpoint) -> None
        Saves a Checkpoint to DynamoDB mce-checkpoints.

    Returns
    -------
    PipelineResult
        Final pipeline result with status, published/escalated files, errors.
    """
    run_timestamp = _now_iso()
    # Generate a unique run_id for this pipeline execution.
    # Used for DynamoDB log keys, checkpoint records, and approval gate tokens.
    run_id = f"{brief.run_date.isoformat() if hasattr(brief, 'run_date') else run_timestamp[:10]}-{brief.topic[:40].lower().replace(' ', '-')}"
    errors: list[dict[str, Any]] = []
    sub_review: SubeditorReview | None = None  # initialise so approval gate guard is safe

    # Bind run_id into a local log helper so every call includes it automatically.
    def _log_run(event_type: str, agent_type: str, details: dict[str, Any]) -> None:
        _log(log_fn, event_type, agent_type, details, run_id=run_id)

    # ------------------------------------------------------------------
    # 1. Validate BullpenBrief
    # ------------------------------------------------------------------
    try:
        validate_brief(brief)
    except BullpenBriefValidationError as exc:
        _log_run("validation_error", "editor_in_chief", {"error": str(exc)})
        return PipelineResult(
            status="error",
            errors=[{"step": "validation", "error": str(exc)}],
            run_timestamp=_now_iso(),
        )

    _log(
        log_fn,
        "brief_accepted",
        "editor_in_chief",
        {
            "topic": brief.topic,
            "requested_outputs": brief.requested_outputs,
            "run_date": str(brief.run_date),
        },
    )

    # ------------------------------------------------------------------
    # 2. Researcher
    # ------------------------------------------------------------------
    _log_run("agent_invoked", "researcher", {"topic": brief.topic})

    try:
        research_brief: ResearchBrief = researcher_fn(brief)
    except Exception as exc:
        err = {"step": "researcher", "error": str(exc)}
        errors.append(err)
        _log_run("agent_error", "researcher", err)
        _checkpoint(checkpoint_fn, "researcher", "", "failure")
        logger.error("Researcher failed — halting pipeline: %s", exc)
        return PipelineResult(
            status="halted",
            errors=errors,
            run_timestamp=_now_iso(),
        )

    research_hash = _hash_dict(dataclasses.asdict(research_brief))
    _log(
        log_fn,
        "agent_completed",
        "researcher",
        {
            # Kept (scored-above-threshold) count. Preserved for backward
            # compatibility with downstream consumers.
            "articles_count": len(research_brief.articles),
            # Console KPI tile counts (Issue #59):
            "articles_crawled": getattr(research_brief, "articles_crawled", 0),
            "scored_above_threshold": len(research_brief.articles),
            "sources_crawled": len(research_brief.sources_crawled),
            "sources_failed": len(research_brief.sources_failed),
            "output_hash": research_hash,
        },
    )
    _checkpoint(checkpoint_fn, "researcher", research_hash, "success")

    # ------------------------------------------------------------------
    # 3. Desk Editor
    # ------------------------------------------------------------------
    _log_run("agent_invoked", "desk_editor", {"topic": brief.topic})

    try:
        content_brief: ContentBrief = desk_editor_fn(
            research_brief, brief.topic, brief.requested_outputs
        )
    except Exception as exc:
        err = {"step": "desk_editor", "error": str(exc)}
        errors.append(err)
        _log_run("agent_error", "desk_editor", err)
        _checkpoint(checkpoint_fn, "desk_editor", "", "failure")
        logger.error("Desk Editor failed — halting pipeline: %s", exc)
        return PipelineResult(
            status="halted",
            errors=errors,
            run_timestamp=_now_iso(),
        )

    desk_hash = _hash_dict(dataclasses.asdict(content_brief))
    _log(
        log_fn,
        "agent_completed",
        "desk_editor",
        {
            "selected_articles": len(content_brief.selected_articles),
            "output_types": content_brief.output_types,
            "output_hash": desk_hash,
        },
    )
    _checkpoint(checkpoint_fn, "desk_editor", desk_hash, "success")

    # ------------------------------------------------------------------
    # 4. Writer (initial pass)
    # ------------------------------------------------------------------
    _log_run("agent_invoked", "writer", {"output_types": content_brief.output_types})

    try:
        writer_manifest: WriterManifest = writer_fn(content_brief, None)
    except Exception as exc:
        err = {"step": "writer", "error": str(exc)}
        errors.append(err)
        _log_run("agent_error", "writer", err)
        _checkpoint(checkpoint_fn, "writer", "", "failure")
        logger.error("Writer failed entirely — halting pipeline: %s", exc)
        return PipelineResult(
            status="halted",
            errors=errors,
            run_timestamp=_now_iso(),
        )

    writer_hash = _hash_dict(dataclasses.asdict(writer_manifest))
    _log(
        log_fn,
        "agent_completed",
        "writer",
        {
            "files_written": len(writer_manifest.files_written),
            "output_hash": writer_hash,
        },
    )
    _checkpoint(checkpoint_fn, "writer", writer_hash, "success")

    # ------------------------------------------------------------------
    # 5. Subeditor + revision loop
    # ------------------------------------------------------------------
    files_escalated: list[str] = []
    files_published: list[str] = []

    # Track revision counts per filename
    revision_counts: dict[str, int] = {}

    current_manifest = writer_manifest

    for cycle in range(MAX_REVISION_CYCLES + 1):
        _log(
            log_fn,
            "agent_invoked",
            "subeditor",
            {"cycle": cycle, "files": len(current_manifest.files_written)},
        )

        try:
            sub_review: SubeditorReview = subeditor_fn(current_manifest)
        except Exception as exc:
            err = {"step": "subeditor", "error": str(exc)}
            errors.append(err)
            _log_run("agent_error", "subeditor", err)
            _checkpoint(checkpoint_fn, "subeditor", "", "failure")
            logger.error("Subeditor failed — escalating all pending files: %s", exc)
            # Mark all pending files for manual review
            for entry in current_manifest.files_written:
                fname = entry.path
                if fname not in files_published and fname not in files_escalated:
                    files_escalated.append(fname)
                    _log(
                        log_fn,
                        "file_escalated",
                        "subeditor",
                        {"filename": fname, "reason": "subeditor_failure"},
                    )
            break

        sub_hash = _hash_dict(dataclasses.asdict(sub_review))
        _log(
            log_fn,
            "agent_completed",
            "subeditor",
            {"cycle": cycle, "verdicts": len(sub_review.verdicts), "output_hash": sub_hash},
        )
        _checkpoint(checkpoint_fn, "subeditor", sub_hash, "success")

        # Log all verdicts
        for v in sub_review.verdicts:
            _log(
                log_fn,
                "verdict",
                "subeditor",
                {
                    "filename": v.filename,
                    "verdict": v.verdict,
                    "feedback": v.feedback,
                    "cycle": cycle,
                },
            )

        # Handle spike verdicts — discard, log rationale, never re-invoke Writer
        for v in _collect_spike_verdicts(sub_review):
            _log(
                log_fn,
                "file_spiked",
                "subeditor",
                {"filename": v.filename, "rationale": v.feedback},
            )
            logger.info("File spiked: %s — %s", v.filename, v.feedback)

        # Collect publish verdicts
        for fname in _collect_publish_filenames(sub_review):
            if fname not in files_published:
                files_published.append(fname)

        # Collect revise verdicts
        revise_filenames = _collect_revise_filenames(sub_review)

        if not revise_filenames:
            # No more revisions needed — exit loop
            break

        # Check which files have hit the revision limit
        still_revising: list[str] = []
        for fname in revise_filenames:
            revision_counts[fname] = revision_counts.get(fname, 0) + 1
            if revision_counts[fname] >= MAX_REVISION_CYCLES:
                # Max revisions reached — escalate
                files_escalated.append(fname)
                _log(
                    log_fn,
                    "file_escalated",
                    "subeditor",
                    {
                        "filename": fname,
                        "reason": "max_revisions_reached",
                        "revision_count": revision_counts[fname],
                    },
                )
                logger.info(
                    "Max revisions reached for %s (count=%d) — escalating",
                    fname,
                    revision_counts[fname],
                )
            else:
                still_revising.append(fname)

        if not still_revising:
            # All revise files have been escalated — exit loop
            break

        if cycle >= MAX_REVISION_CYCLES:
            # Safety guard: should not reach here, but escalate any remaining
            for fname in still_revising:
                if fname not in files_escalated:
                    files_escalated.append(fname)
                    _log(
                        log_fn,
                        "file_escalated",
                        "subeditor",
                        {
                            "filename": fname,
                            "reason": "max_revisions_reached",
                            "revision_count": revision_counts.get(fname, 0),
                        },
                    )
            break

        # Re-invoke Writer with feedback for files still needing revision
        revision_feedback = _build_revision_feedback(sub_review)
        _log(
            log_fn,
            "agent_invoked",
            "writer",
            {
                "cycle": cycle + 1,
                "revision_files": still_revising,
                "feedback_length": len(revision_feedback),
            },
        )

        try:
            current_manifest = writer_fn(content_brief, revision_feedback)
        except Exception as exc:
            err = {"step": "writer_revision", "cycle": cycle + 1, "error": str(exc)}
            errors.append(err)
            _log_run("agent_error", "writer", err)
            _checkpoint(checkpoint_fn, "writer", "", "failure")
            logger.error("Writer revision cycle %d failed: %s", cycle + 1, exc)
            # Escalate all files that were still being revised
            for fname in still_revising:
                if fname not in files_escalated:
                    files_escalated.append(fname)
                    _log(
                        log_fn,
                        "file_escalated",
                        "writer",
                        {"filename": fname, "reason": "writer_revision_failure"},
                    )
            break

        rev_hash = _hash_dict(dataclasses.asdict(current_manifest))
        _log(
            log_fn,
            "agent_completed",
            "writer",
            {
                "cycle": cycle + 1,
                "files_written": len(current_manifest.files_written),
                "output_hash": rev_hash,
            },
        )
        _checkpoint(checkpoint_fn, "writer", rev_hash, "success")

    # ------------------------------------------------------------------
    # 6. Approval gate
    # ------------------------------------------------------------------
    if files_published:
        _log(
            log_fn,
            "approval_gate_presented",
            "editor_in_chief",
            {"files_pending_approval": files_published},
        )

        approved = approval_fn(sub_review)

        _log(
            log_fn,
            "approval_decision",
            "editor_in_chief",
            {"approved": approved, "files": files_published},
        )

        if approved:
            # ------------------------------------------------------------------
            # 7. Publisher
            # ------------------------------------------------------------------
            _log(
                log_fn,
                "agent_invoked",
                "publisher",
                {"files": files_published},
            )

            try:
                publisher_fn(files_published)
            except Exception as exc:
                err = {"step": "publisher", "error": str(exc)}
                errors.append(err)
                _log_run("agent_error", "publisher", err)
                _checkpoint(checkpoint_fn, "publisher", "", "failure")
                logger.error("Publisher failed: %s", exc)
                return PipelineResult(
                    status="error",
                    files_published=[],
                    files_escalated=files_escalated,
                    errors=errors,
                    run_timestamp=_now_iso(),
                )

            pub_hash = _hash_dict({"files": files_published})
            _log(
                log_fn,
                "agent_completed",
                "publisher",
                {"files_published": files_published, "output_hash": pub_hash},
            )
            _checkpoint(checkpoint_fn, "publisher", pub_hash, "success")
        else:
            # Rejected — retain files in S3, do not publish
            _log(
                log_fn,
                "approval_rejected",
                "editor_in_chief",
                {"files_retained": files_published},
            )
            files_published = []

    # ------------------------------------------------------------------
    # 8. Final result
    # ------------------------------------------------------------------
    status = "success" if not errors else "error"
    if not files_published and not files_escalated and errors:
        status = "halted"

    return PipelineResult(
        status=status,
        files_published=files_published,
        files_escalated=files_escalated,
        errors=errors,
        run_timestamp=_now_iso(),
    )
