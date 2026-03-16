"""Engagement signal tracking via the dev.to API.

Fetches per-post engagement metrics (views, reactions, comments,
reading_time) from dev.to, stores PostEngagement records in
AgentCore Memory (long-term), and identifies the top performing
post from the past 7 days.

The actual dev.to HTTP calls and AgentCore Memory integration are
injected via protocols so that tests can supply lightweight stubs.

Requirements: REQ-034.1, REQ-034.2, REQ-034.4, REQ-034.5
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional, Protocol, runtime_checkable

from magic_content_engine.config import DEVTO_API_KEY, DEVTO_USERNAME
from magic_content_engine.errors import ErrorCollector, StepError
from magic_content_engine.models import PostEngagement

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols (dependency injection for testability)
# ---------------------------------------------------------------------------


@runtime_checkable
class DevToAPIProtocol(Protocol):
    """Interface for fetching articles from the dev.to API.

    In production this issues GET https://dev.to/api/articles?username={username}
    with the configured API key. Tests supply a stub.
    """

    def fetch_user_articles(self, username: str, api_key: str) -> list[dict]:
        """Return a list of article dicts from the dev.to API.

        Each dict should contain at minimum:
        - title (str)
        - published_at (str, ISO date)
        - url (str)
        - page_views_count (int)
        - positive_reactions_count (int)
        - comments_count (int)
        - reading_time_minutes (int)

        Raises on network/API errors.
        """
        ...


@runtime_checkable
class EngagementMemoryProtocol(Protocol):
    """Interface for persisting PostEngagement records.

    In production this is backed by AgentCore Memory (long-term),
    keyed by post title and publication date. Tests supply a stub.
    """

    def load_engagements(self) -> list[PostEngagement]:
        """Load all stored PostEngagement records."""
        ...

    def save_engagements(self, engagements: list[PostEngagement]) -> None:
        """Persist PostEngagement records to long-term memory."""
        ...


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_article(article: dict, fetched_date: date) -> PostEngagement:
    """Convert a raw dev.to API article dict to a PostEngagement."""
    published_str = article.get("published_at", "")
    try:
        publication_date = date.fromisoformat(published_str[:10])
    except (ValueError, TypeError):
        publication_date = fetched_date

    return PostEngagement(
        post_title=article.get("title", ""),
        publication_date=publication_date,
        url=article.get("url", ""),
        views=article.get("page_views_count", 0) or 0,
        reactions=article.get("positive_reactions_count", 0) or 0,
        comments=article.get("comments_count", 0) or 0,
        reading_time_minutes=article.get("reading_time_minutes", 0) or 0,
        last_fetched=fetched_date,
    )


def _engagement_key(engagement: PostEngagement) -> tuple[str, date]:
    """Unique key for a PostEngagement: (post_title, publication_date)."""
    return (engagement.post_title, engagement.publication_date)


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def fetch_engagement_metrics(
    api: DevToAPIProtocol,
    username: str = DEVTO_USERNAME,
    api_key: str = DEVTO_API_KEY,
    run_date: Optional[date] = None,
) -> list[PostEngagement]:
    """Fetch engagement metrics from the dev.to API.

    Returns a list of PostEngagement records parsed from the API
    response. Raises on network/API errors (caller handles).
    """
    if run_date is None:
        run_date = date.today()

    raw_articles = api.fetch_user_articles(username, api_key)
    engagements = [_parse_article(article, run_date) for article in raw_articles]
    logger.info("Fetched %d engagement records from dev.to for user '%s'", len(engagements), username)
    return engagements


def store_engagements(
    memory: EngagementMemoryProtocol,
    new_engagements: list[PostEngagement],
) -> list[PostEngagement]:
    """Merge new engagement data into long-term memory.

    Existing records are updated with fresh metrics; new posts are
    appended. Returns the merged list.
    """
    existing = memory.load_engagements()
    existing_by_key: dict[tuple[str, date], PostEngagement] = {
        _engagement_key(e): e for e in existing
    }

    for eng in new_engagements:
        key = _engagement_key(eng)
        existing_by_key[key] = eng  # overwrite with latest metrics

    merged = list(existing_by_key.values())
    memory.save_engagements(merged)
    logger.info("Stored %d engagement records in long-term memory", len(merged))
    return merged


def identify_top_post(
    engagements: list[PostEngagement],
    run_date: Optional[date] = None,
    window_days: int = 7,
) -> Optional[PostEngagement]:
    """Find the top performing post from the past *window_days* days.

    Top performing = highest (views + reactions) among posts published
    within the window. Returns None if no posts fall in the window.
    """
    if run_date is None:
        run_date = date.today()

    cutoff = run_date - timedelta(days=window_days)
    recent = [e for e in engagements if e.publication_date >= cutoff]

    if not recent:
        logger.info("No posts found in the past %d days", window_days)
        return None

    top = max(recent, key=lambda e: e.views + e.reactions)
    logger.info(
        "Top post (past %d days): '%s' — %d views, %d reactions",
        window_days,
        top.post_title,
        top.views,
        top.reactions,
    )
    return top


# ---------------------------------------------------------------------------
# Orchestrated engagement fetch flow with clean state handling
# ---------------------------------------------------------------------------


def fetch_engagement_flow(
    api: DevToAPIProtocol,
    memory: EngagementMemoryProtocol,
    collector: ErrorCollector,
    username: str = DEVTO_USERNAME,
    api_key: str = DEVTO_API_KEY,
    run_date: Optional[date] = None,
) -> list[PostEngagement]:
    """Orchestrate the full engagement metric fetch with clean state detection.

    1. Attempt to fetch engagement metrics from the dev.to API.
    2. If the API returns an empty list (no posts): log "no published
       content yet", return empty list, no error raised.
    3. If the API raises an exception (unreachable): log the failure
       via *collector*, return empty list, continue.
    4. On success with posts: store engagements in long-term memory
       and return the merged list.

    All fetches and failures are recorded for the Agent_Log.

    Requirements: REQ-034.6, REQ-034.7, REQ-034.8, REQ-034.9
    """
    if run_date is None:
        run_date = date.today()

    # --- Attempt API fetch ---
    try:
        engagements = fetch_engagement_metrics(
            api, username=username, api_key=api_key, run_date=run_date,
        )
    except Exception as exc:
        # API unreachable — log failure, skip engagement tracking, continue
        collector.add(
            StepError(
                step="engagement",
                target="dev.to API",
                error_message=str(exc),
                context={"action": "fetch_engagement_metrics", "username": username},
            )
        )
        logger.warning("dev.to API unreachable — skipping engagement tracking")
        return []

    # --- Clean state: no published posts ---
    if not engagements:
        logger.info("no published content yet")
        return []

    # --- Posts found: store in long-term memory ---
    merged = store_engagements(memory, engagements)
    logger.info(
        "Engagement flow complete: fetched %d, stored %d records",
        len(engagements),
        len(merged),
    )
    return merged
