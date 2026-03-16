"""Relevance scoring pipeline for discovered articles.

Sends each article to Claude Haiku for relevance scoring on a 1-5 scale,
filters by configurable threshold, and records scores in the Agent_Log.

When engagement metrics are available (REQ-034.3), articles whose
topics match high-performing past content receive a score boost.
When no metrics exist (clean state, REQ-034.10), scoring relies
solely on relevance criteria and topic gap analysis.

Requirements: REQ-005.1, REQ-005.2, REQ-005.3, REQ-005.4, REQ-027.2,
              REQ-034.3, REQ-034.10
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Protocol

from magic_content_engine import config
from magic_content_engine.errors import ErrorCollector, StepError
from magic_content_engine.models import Article, PostEngagement
from magic_content_engine.model_router import TaskType, get_model

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scoring criteria prompt (REQ-005.2)
# ---------------------------------------------------------------------------

SCORING_PROMPT_TEMPLATE = """\
You are a relevance scorer for an AI Engineering content niche focused on \
Kiro IDE, AgentCore, Strands Agents SDK, and Bedrock — from the Aotearoa / \
Oceania community perspective.

Score the following article on a 1-to-5 integer scale:

High (4-5): Kiro IDE features or breaking changes, AgentCore/Strands/Bedrock \
announcements, MCP spec updates, steering docs or Kiro extension ecosystem \
news, Community Builder programme news, changes affecting \
kiro-steering-docs-extension directly.

Medium (3): AWS Lambda/S3/IAM changes affecting agent deployments, general \
agentic AI patterns with AWS application, NZ/Oceania AWS events or community news.

Low (1-2): Generic AI news without AWS angle, AWS services with no agent relevance.

Article title: {title}
Article URL: {url}
Article source: {source}
Source type: {source_type}

Respond with ONLY a JSON object (no markdown, no extra text):
{{
  "score": <integer 1-5>,
  "rationale": "<one sentence explaining the score>"
}}
"""


# ---------------------------------------------------------------------------
# LLM scorer protocol — allows test doubles
# ---------------------------------------------------------------------------


class LLMScorer(Protocol):
    """Protocol for the LLM call used during relevance scoring.

    Production implementations call Bedrock via the Strands SDK.
    Tests can supply a simple callable that returns a JSON string.
    """

    def __call__(self, prompt: str, model_id: str) -> str:
        """Send *prompt* to the model identified by *model_id*.

        Returns the raw text response from the model.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_score_response(raw: str) -> tuple[int, str]:
    """Extract (score, rationale) from the LLM JSON response.

    Raises ``ValueError`` when the response cannot be parsed or the
    score is outside the valid 1-5 range.
    """
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM response is not valid JSON: {raw!r}") from exc

    score = data.get("score")
    rationale = data.get("rationale", "")

    if not isinstance(score, int) or score < 1 or score > 5:
        raise ValueError(
            f"Score must be an integer in [1, 5], got {score!r}"
        )

    return score, str(rationale)


# ---------------------------------------------------------------------------
# Single-article scoring
# ---------------------------------------------------------------------------


def _score_article(
    article: Article,
    llm: LLMScorer,
    model_id: str,
) -> tuple[int, str]:
    """Score a single article via the LLM.

    Returns (score, rationale).  Raises on LLM or parsing failure.
    """
    prompt = SCORING_PROMPT_TEMPLATE.format(
        title=article.title,
        url=article.url,
        source=article.source,
        source_type=article.source_type,
    )
    raw_response = llm(prompt, model_id)
    return _parse_score_response(raw_response)


# ---------------------------------------------------------------------------
# Engagement-weighted scoring helpers (REQ-034.3, REQ-034.10)
# ---------------------------------------------------------------------------

# Maximum additive boost applied when an article matches a high-engagement topic.
_ENGAGEMENT_BOOST: int = 1


def _build_engagement_keywords(metrics: list[PostEngagement]) -> set[str]:
    """Extract lowercase keywords from high-engagement post titles.

    A post is considered high-engagement when its combined
    (views + reactions) is above the median of the provided set.
    Returns an empty set when *metrics* is empty.
    """
    if not metrics:
        return set()

    scores = sorted(e.views + e.reactions for e in metrics)
    median = scores[len(scores) // 2]

    keywords: set[str] = set()
    for eng in metrics:
        if eng.views + eng.reactions >= median:
            for word in eng.post_title.lower().split():
                # Keep only meaningful words (length > 2)
                cleaned = "".join(ch for ch in word if ch.isalnum())
                if len(cleaned) > 2:
                    keywords.add(cleaned)
    return keywords


def _compute_engagement_boost(
    article: Article,
    engagement_keywords: set[str],
) -> int:
    """Return an additive score boost (0 or ``_ENGAGEMENT_BOOST``).

    The boost is applied when the article title contains at least one
    keyword extracted from high-engagement past posts.
    """
    if not engagement_keywords:
        return 0

    title_words = {
        "".join(ch for ch in w if ch.isalnum())
        for w in article.title.lower().split()
    }
    if title_words & engagement_keywords:
        return _ENGAGEMENT_BOOST
    return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_articles(
    articles: list[Article],
    llm: LLMScorer,
    collector: ErrorCollector | None = None,
    threshold: int | None = None,
    engagement_metrics: Optional[list[PostEngagement]] = None,
) -> list[Article]:
    """Score a list of articles and return those meeting the threshold.

    Articles at or above *threshold* get ``status="scored"``.
    Articles below *threshold* get ``status="excluded"``.
    Only articles with ``status="scored"`` are returned.

    On per-article scoring failure the error is recorded in *collector*
    and the article is skipped (log-and-continue pattern, REQ-027.2).

    Parameters
    ----------
    articles:
        Discovered articles to score.
    llm:
        Callable matching the :class:`LLMScorer` protocol.
    collector:
        Optional error collector.  A fresh one is created when *None*.
    threshold:
        Minimum score to keep.  Defaults to ``config.RELEVANCE_THRESHOLD``.
    engagement_metrics:
        Optional list of :class:`PostEngagement` records.  When provided
        and non-empty, articles whose topics match high-performing past
        content receive an additive score boost (REQ-034.3).  When
        *None* or empty, engagement weighting is skipped entirely and
        scoring relies solely on relevance criteria (REQ-034.10).
    """
    if collector is None:
        collector = ErrorCollector()
    if threshold is None:
        threshold = config.RELEVANCE_THRESHOLD

    model_id = get_model(TaskType.RELEVANCE_SCORING)

    # Pre-compute engagement keywords once (empty set when no metrics)
    engagement_keywords = _build_engagement_keywords(engagement_metrics or [])

    scored: list[Article] = []

    for article in articles:
        try:
            score, rationale = _score_article(article, llm, model_id)

            # Apply engagement boost when metrics exist (REQ-034.3)
            boost = _compute_engagement_boost(article, engagement_keywords)
            if boost:
                score = min(score + boost, 5)
                rationale = f"{rationale} [+{boost} engagement boost]"
                logger.info(
                    "Engagement boost +%d applied to '%s'", boost, article.title,
                )

            article.relevance_score = score
            article.scoring_rationale = rationale

            if score >= threshold:
                article.status = "scored"
                scored.append(article)
                logger.info(
                    "Article scored %d (kept): %s", score, article.url,
                )
            else:
                article.status = "excluded"
                logger.info(
                    "Article scored %d (excluded): %s", score, article.url,
                )
        except Exception as exc:
            collector.add(
                StepError(
                    step="score",
                    target=article.url,
                    error_message=str(exc),
                    context={"model": model_id},
                )
            )
            # Skip this article and continue (REQ-027.2)

    return scored
