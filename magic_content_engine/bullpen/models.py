"""Bullpen pipeline data models.

These dataclasses represent the inputs and outputs of each agent in the
bullpen pipeline: Writer → Subeditor → Publisher.

Requirements: REQ-8.2, REQ-9.4, REQ-10.2
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Writer Agent output
# ---------------------------------------------------------------------------


@dataclass
class FileEntry:
    """A single file written by the Writer Agent."""

    path: str  # relative to output/
    output_type: str  # blog, youtube, cfp, usergroup, digest
    word_count: int


@dataclass
class WriterManifest:
    """Output of the Writer Agent — lists every file it produced."""

    files_written: list[FileEntry]
    voice_rules_applied: bool = True  # always True per Writer contract
    run_timestamp: str = ""  # ISO 8601


# ---------------------------------------------------------------------------
# Subeditor Agent output
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    """Subeditor's assessment of a single content file.

    verdict is exactly one of: "publish" | "revise" | "spike"
    feedback is non-empty for revise/spike, empty string for publish.
    """

    filename: str
    verdict: str  # "publish" | "revise" | "spike"
    feedback: str  # specific feedback for revise, rationale for spike, "" for publish


@dataclass
class SubeditorReview:
    """Output of the Subeditor Agent — one Verdict per input file."""

    verdicts: list[Verdict]
    run_timestamp: str  # ISO 8601
