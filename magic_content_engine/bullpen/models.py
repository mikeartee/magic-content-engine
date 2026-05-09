"""Data models for the Bullpen architecture pipeline.

These complement the existing models in ``magic_content_engine/models.py``
and cover the full agent pipeline: BullpenBrief ΓåÆ ResearchBrief ΓåÆ
ContentBrief ΓåÆ WriterManifest ΓåÆ SubeditorReview ΓåÆ PublicationReport.

Requirements: REQ-bullpen-13.2, REQ-bullpen-15.3, REQ-bullpen-16.1
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any


# ---------------------------------------------------------------------------
# Input brief
# ---------------------------------------------------------------------------


@dataclass
class BullpenBrief:
    """Input to the Editor-in-Chief to initiate a content run.

    Named BullpenBrief (not WeeklyBrief) to avoid collision with the
    existing WeeklyBrief dataclass in magic_content_engine/models.py.
    """

    topic: str
    requested_outputs: list[str]
    run_date: date = field(default_factory=date.today)


# ---------------------------------------------------------------------------
# Researcher output
# ---------------------------------------------------------------------------


@dataclass
class ScoredArticle:
    """A single article with relevance score from the Researcher Agent."""

    title: str
    url: str
    source: str
    relevance_score: int  # 1-5
    summary: str  # one-sentence summary


@dataclass
class ResearchBrief:
    """Output of the Researcher Agent."""

    articles: list[ScoredArticle]
    sources_crawled: list[str]
    sources_failed: list[str]
    run_timestamp: str  # ISO 8601

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict."""
        return {
            "articles": [asdict(a) for a in self.articles],
            "sources_crawled": list(self.sources_crawled),
            "sources_failed": list(self.sources_failed),
            "run_timestamp": self.run_timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchBrief":
        """Reconstruct a ResearchBrief from a plain dict (round-trip)."""
        return cls(
            articles=[ScoredArticle(**a) for a in data["articles"]],
            sources_crawled=list(data["sources_crawled"]),
            sources_failed=list(data["sources_failed"]),
            run_timestamp=data["run_timestamp"],
        )


# ---------------------------------------------------------------------------
# Desk Editor output
# ---------------------------------------------------------------------------


@dataclass
class ContentBrief:
    """Output of the Desk Editor Agent."""

    selected_articles: list[ScoredArticle]
    editorial_angle: str
    tone_guidance: str
    output_types: list[str]
    run_timestamp: str  # ISO 8601
    slug: str = ""       # kebab-case topic slug; populated by Desk Editor
    run_date: str = ""   # YYYY-MM-DD; defaults to today in Writer if empty


# ---------------------------------------------------------------------------
# Writer output
# ---------------------------------------------------------------------------


@dataclass
class FileEntry:
    """A single file written by the Writer Agent."""

    path: str
    output_type: str
    word_count: int


@dataclass
class WriterManifest:
    """Output of the Writer Agent."""

    files_written: list[FileEntry]
    voice_rules_applied: bool = True
    run_timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (JSON-round-trip safe)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WriterManifest":
        """Deserialise from a plain dict."""
        files = [FileEntry(**f) for f in data.get("files_written", [])]
        return cls(
            files_written=files,
            voice_rules_applied=data.get("voice_rules_applied", True),
            run_timestamp=data.get("run_timestamp", ""),
        )


@dataclass
class WriterInput:
    """Input to the Writer Agent, including optional revision feedback."""

    content_brief: ContentBrief
    steering_base_path: str
    output_dir: str
    revision_feedback: str | None = None


# ---------------------------------------------------------------------------
# Subeditor output
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    """Subeditor's assessment of a single content file."""

    filename: str
    verdict: str  # "publish" | "revise" | "spike"
    feedback: str  # specific feedback for revise, rationale for spike, empty for publish


@dataclass
class SubeditorReview:
    """Output of the Subeditor Agent."""

    verdicts: list[Verdict]
    run_timestamp: str  # ISO 8601


# ---------------------------------------------------------------------------
# Publisher output
# ---------------------------------------------------------------------------


@dataclass
class UploadedFile:
    """A single file successfully uploaded to S3."""

    local_path: str
    s3_key: str


@dataclass
class PublicationReport:
    """Output of the Publisher Agent.

    Requirements: REQ-bullpen-13.2
    """

    files_uploaded: list[UploadedFile]
    email_sent: bool
    email_recipient: str
    run_timestamp: str  # ISO 8601

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (JSON-compatible)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PublicationReport":
        """Deserialise from a plain dict."""
        files = [UploadedFile(**f) for f in data["files_uploaded"]]
        return cls(
            files_uploaded=files,
            email_sent=data["email_sent"],
            email_recipient=data["email_recipient"],
            run_timestamp=data["run_timestamp"],
        )

    def to_json(self) -> str:
        """Serialise to a JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> "PublicationReport":
        """Deserialise from a JSON string."""
        return cls.from_dict(json.loads(s))


# ---------------------------------------------------------------------------
# Checkpoint and AMI log
# ---------------------------------------------------------------------------

AGENT_TYPES = [
    "researcher",
    "desk_editor",
    "writer",
    "subeditor",
    "publisher",
    "archivist",
]


@dataclass
class Checkpoint:
    """Progress record written after each agent completes."""

    agent_type: str  # one of AGENT_TYPES
    completion_timestamp: str  # ISO 8601
    output_hash: str
    status: str  # "success" | "failure"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Checkpoint":
        return cls(**data)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> "Checkpoint":
        return cls.from_dict(json.loads(s))


@dataclass
class AMILogEvent:
    """A single structured event in the AMI decision log."""

    event_type: str
    timestamp: str  # ISO 8601
    agent_type: str
    run_id: str = ""  # pipeline run identifier; set by Editor-in-Chief
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AMILogEvent":
        return cls(**data)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> "AMILogEvent":
        return cls.from_dict(json.loads(s))
