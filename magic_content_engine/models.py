"""Core data models for the Magic Content Engine.

All dataclasses match the design document exactly, with type hints
and default values as specified.

Requirements: REQ-005.1, REQ-006.1–REQ-006.3, REQ-007.1–REQ-007.3,
REQ-016.1, REQ-017.1–REQ-017.4, REQ-026.1–REQ-026.3, REQ-030.5,
REQ-033.1, REQ-034.1–REQ-034.2, REQ-035.2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ---------------------------------------------------------------------------
# Article pipeline
# ---------------------------------------------------------------------------


@dataclass
class Article:
    """A single news item discovered during a research crawl."""

    url: str
    title: str
    source: str  # e.g. "kiro.dev/changelog/ide/"
    source_type: str  # "primary" | "secondary"
    discovered_date: date
    relevance_score: Optional[int] = None  # 1-5, assigned by Haiku
    scoring_rationale: Optional[str] = None
    status: str = "discovered"  # discovered | scored | excluded | confirmed | previously_covered


@dataclass
class ArticleMetadata:
    """Structured metadata extracted from an article page."""

    article_url: str
    title: str  # from og:title or HTML title
    publication_date: Optional[str] = None  # from og:published_time
    author: str = "Amazon Web Services"  # fallback default
    publisher: str = "Amazon Web Services"  # fallback default
    canonical_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------


@dataclass
class APACitation:
    """An APA 7th edition citation built from article metadata."""

    metadata: ArticleMetadata
    reference_entry: str  # Full APA 7th reference string
    in_text_citation: str  # (Surname, Year) format
    bibtex_entry: str  # @online{} BibTeX block


# ---------------------------------------------------------------------------
# Cost tracking
# ---------------------------------------------------------------------------


@dataclass
class ModelInvocation:
    """A single model call with token counts and cost."""

    task_type: str
    model: str  # "claude-haiku" | "claude-sonnet"
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


@dataclass
class CostEstimate:
    """Aggregated cost breakdown for a run."""

    invocations: list[ModelInvocation]
    total_llm_cost_usd: float
    total_agentcore_cost_usd: float
    total_cost_usd: float


# ---------------------------------------------------------------------------
# Agent log
# ---------------------------------------------------------------------------


@dataclass
class AgentLog:
    """Run-level log capturing articles, model usage, and errors."""

    run_date: str
    invocation_source: str  # "scheduled" | "manual"
    articles_found: int
    articles_kept: int
    articles: list[dict]  # per-article: url, score, status
    model_usage: list[dict]  # per-task: task, model, tokens
    screenshot_results: list[dict]  # per-screenshot: filename, success, error
    errors: list[dict]  # step, message, context
    selected_outputs: list[str]
    run_metadata: dict  # timing, versions, etc.


# ---------------------------------------------------------------------------
# Screenshot capture
# ---------------------------------------------------------------------------


@dataclass
class ScreenshotCapture:
    """Configuration and result for a single screenshot capture."""

    target_url: str
    filename: str  # e.g. "console-runtime.png"
    viewport_width: int = 1440
    viewport_height: int = 900
    wait_seconds: int = 3
    success: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Output bundle
# ---------------------------------------------------------------------------


@dataclass
class OutputBundle:
    """The complete output directory produced by a single run."""

    run_date: date
    slug: str
    selected_outputs: list[str]  # ["blog", "youtube", "cfp", ...]
    generated_files: list[str]  # relative paths of files created
    references_bib: str  # aggregated BibTeX content
    cost_estimate: CostEstimate
    agent_log: AgentLog
    s3_key_prefix: str  # "output/YYYY-MM-DD-[slug]/"


# ---------------------------------------------------------------------------
# Publish Gate: held and review items
# ---------------------------------------------------------------------------


@dataclass
class HeldItem:
    """A content output held for embargo release."""

    filename: str  # e.g. "post.md"
    s3_destination_path: str  # e.g. "output/2025-07-14-agentcore-browser-launch/post.md"
    release_date: date  # embargo release date (YYYY-MM-DD)
    article_titles: list[str]  # titles of articles covered in this output
    run_date: date  # date of the run that generated this output
    local_file_path: str  # e.g. "./output/held/2025-07-14-agentcore-browser-launch/post.md"


@dataclass
class ReviewItem:
    """A content output held for manual review."""

    filename: str  # e.g. "cfp-proposal.md"
    run_date: date  # date of the run that generated this output
    local_file_path: str  # e.g. "./output/review/2025-07-14-agentcore-browser-launch/cfp-proposal.md"
    reason: str  # e.g. "held for manual review"


# ---------------------------------------------------------------------------
# Topic coverage
# ---------------------------------------------------------------------------


@dataclass
class TopicCoverageEntry:
    """Coverage status for a single niche topic."""

    topic: str  # e.g. "AgentCore Runtime"
    covered: bool
    article_titles: list[str]  # titles of articles that covered this topic
    last_covered_date: Optional[date] = None
    adjacent_topics: list[str] = field(default_factory=list)  # natural next topics


@dataclass
class TopicCoverageMap:
    """Persistent map of topic coverage across runs."""

    entries: list[TopicCoverageEntry]
    last_updated: date
    recommended_focus: Optional[str] = None  # set at start of each run


# ---------------------------------------------------------------------------
# Engagement tracking
# ---------------------------------------------------------------------------


@dataclass
class PostEngagement:
    """Engagement metrics for a single published post."""

    post_title: str
    publication_date: date
    url: str
    views: int = 0
    reactions: int = 0
    comments: int = 0
    reading_time_minutes: int = 0
    last_fetched: Optional[date] = None


# ---------------------------------------------------------------------------
# Weekly brief
# ---------------------------------------------------------------------------


@dataclass
class WeeklyBrief:
    """Personalised run summary presented before the research crawl."""

    run_date: date
    top_post: Optional[PostEngagement]  # None if no published content yet
    coverage_map: TopicCoverageMap
    recommended_focus: str
    user_override: Optional[str] = None  # set if user overrides recommendation
    clean_state: bool = False  # True if no published content yet
