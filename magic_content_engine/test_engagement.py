"""Unit tests for engagement signal tracking.

Tests cover:
- Parsing dev.to API responses into PostEngagement records
- Storing and merging engagement records in memory
- Identifying top performing post from past 7 days
- Edge cases: empty responses, missing fields, date parsing
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import pytest

from magic_content_engine.engagement import (
    DevToAPIProtocol,
    EngagementMemoryProtocol,
    _engagement_key,
    _parse_article,
    fetch_engagement_flow,
    fetch_engagement_metrics,
    identify_top_post,
    store_engagements,
)
from magic_content_engine.errors import ErrorCollector
from magic_content_engine.models import PostEngagement


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubDevToAPI:
    """In-memory stub implementing DevToAPIProtocol."""

    def __init__(self, articles: Optional[list[dict]] = None, error: Optional[Exception] = None) -> None:
        self._articles = articles or []
        self._error = error
        self.call_count = 0

    def fetch_user_articles(self, username: str, api_key: str) -> list[dict]:
        self.call_count += 1
        if self._error:
            raise self._error
        return self._articles


class StubEngagementMemory:
    """In-memory stub implementing EngagementMemoryProtocol."""

    def __init__(self, stored: Optional[list[PostEngagement]] = None) -> None:
        self._stored: list[PostEngagement] = stored or []
        self.save_count = 0

    def load_engagements(self) -> list[PostEngagement]:
        return list(self._stored)

    def save_engagements(self, engagements: list[PostEngagement]) -> None:
        self._stored = engagements
        self.save_count += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RUN_DATE = date(2025, 7, 14)

SAMPLE_ARTICLE = {
    "title": "Building Agents with Strands SDK",
    "published_at": "2025-07-10T09:00:00Z",
    "url": "https://dev.to/mike/building-agents",
    "page_views_count": 250,
    "positive_reactions_count": 42,
    "comments_count": 7,
    "reading_time_minutes": 5,
}


def _make_engagement(
    title: str = "Test Post",
    pub_date: Optional[date] = None,
    views: int = 100,
    reactions: int = 10,
    comments: int = 2,
    reading_time: int = 4,
) -> PostEngagement:
    return PostEngagement(
        post_title=title,
        publication_date=pub_date or RUN_DATE,
        url=f"https://dev.to/mike/{title.lower().replace(' ', '-')}",
        views=views,
        reactions=reactions,
        comments=comments,
        reading_time_minutes=reading_time,
        last_fetched=RUN_DATE,
    )


# ---------------------------------------------------------------------------
# _parse_article tests
# ---------------------------------------------------------------------------


class TestParseArticle:
    def test_parses_complete_article(self) -> None:
        result = _parse_article(SAMPLE_ARTICLE, RUN_DATE)
        assert result.post_title == "Building Agents with Strands SDK"
        assert result.publication_date == date(2025, 7, 10)
        assert result.url == "https://dev.to/mike/building-agents"
        assert result.views == 250
        assert result.reactions == 42
        assert result.comments == 7
        assert result.reading_time_minutes == 5
        assert result.last_fetched == RUN_DATE

    def test_handles_missing_fields(self) -> None:
        result = _parse_article({}, RUN_DATE)
        assert result.post_title == ""
        assert result.publication_date == RUN_DATE  # fallback
        assert result.views == 0
        assert result.reactions == 0
        assert result.comments == 0

    def test_handles_none_counts(self) -> None:
        article = {**SAMPLE_ARTICLE, "page_views_count": None, "positive_reactions_count": None}
        result = _parse_article(article, RUN_DATE)
        assert result.views == 0
        assert result.reactions == 0

    def test_handles_invalid_date(self) -> None:
        article = {**SAMPLE_ARTICLE, "published_at": "not-a-date"}
        result = _parse_article(article, RUN_DATE)
        assert result.publication_date == RUN_DATE  # fallback


# ---------------------------------------------------------------------------
# fetch_engagement_metrics tests
# ---------------------------------------------------------------------------


class TestFetchEngagementMetrics:
    def test_returns_parsed_engagements(self) -> None:
        api = StubDevToAPI(articles=[SAMPLE_ARTICLE])
        result = fetch_engagement_metrics(api, username="mike", api_key="key", run_date=RUN_DATE)
        assert len(result) == 1
        assert result[0].post_title == "Building Agents with Strands SDK"

    def test_empty_response(self) -> None:
        api = StubDevToAPI(articles=[])
        result = fetch_engagement_metrics(api, username="mike", api_key="key", run_date=RUN_DATE)
        assert result == []

    def test_propagates_api_error(self) -> None:
        api = StubDevToAPI(error=ConnectionError("unreachable"))
        with pytest.raises(ConnectionError):
            fetch_engagement_metrics(api, username="mike", api_key="key", run_date=RUN_DATE)


# ---------------------------------------------------------------------------
# store_engagements tests
# ---------------------------------------------------------------------------


class TestStoreEngagements:
    def test_stores_new_engagements(self) -> None:
        memory = StubEngagementMemory()
        eng = _make_engagement("Post A")
        result = store_engagements(memory, [eng])
        assert len(result) == 1
        assert memory.save_count == 1

    def test_merges_with_existing(self) -> None:
        existing = _make_engagement("Post A", views=50)
        memory = StubEngagementMemory(stored=[existing])
        updated = _make_engagement("Post A", views=200)
        result = store_engagements(memory, [updated])
        assert len(result) == 1
        assert result[0].views == 200  # updated

    def test_preserves_unrelated_posts(self) -> None:
        existing = _make_engagement("Post A")
        memory = StubEngagementMemory(stored=[existing])
        new = _make_engagement("Post B")
        result = store_engagements(memory, [new])
        assert len(result) == 2
        titles = {e.post_title for e in result}
        assert titles == {"Post A", "Post B"}


# ---------------------------------------------------------------------------
# identify_top_post tests
# ---------------------------------------------------------------------------


class TestIdentifyTopPost:
    def test_finds_top_post_in_window(self) -> None:
        posts = [
            _make_engagement("Low", pub_date=RUN_DATE - timedelta(days=2), views=10, reactions=5),
            _make_engagement("High", pub_date=RUN_DATE - timedelta(days=1), views=500, reactions=80),
            _make_engagement("Medium", pub_date=RUN_DATE - timedelta(days=3), views=100, reactions=20),
        ]
        top = identify_top_post(posts, run_date=RUN_DATE)
        assert top is not None
        assert top.post_title == "High"

    def test_excludes_posts_outside_window(self) -> None:
        old_post = _make_engagement("Old", pub_date=RUN_DATE - timedelta(days=10), views=9999, reactions=9999)
        recent_post = _make_engagement("Recent", pub_date=RUN_DATE - timedelta(days=2), views=10, reactions=5)
        top = identify_top_post([old_post, recent_post], run_date=RUN_DATE)
        assert top is not None
        assert top.post_title == "Recent"

    def test_returns_none_when_no_recent_posts(self) -> None:
        old = _make_engagement("Old", pub_date=RUN_DATE - timedelta(days=30))
        assert identify_top_post([old], run_date=RUN_DATE) is None

    def test_returns_none_for_empty_list(self) -> None:
        assert identify_top_post([], run_date=RUN_DATE) is None

    def test_boundary_exactly_7_days_ago(self) -> None:
        boundary = _make_engagement("Boundary", pub_date=RUN_DATE - timedelta(days=7), views=100, reactions=50)
        top = identify_top_post([boundary], run_date=RUN_DATE)
        assert top is not None
        assert top.post_title == "Boundary"


# ---------------------------------------------------------------------------
# _engagement_key tests
# ---------------------------------------------------------------------------


class TestEngagementKey:
    def test_key_is_title_and_date(self) -> None:
        eng = _make_engagement("My Post", pub_date=date(2025, 7, 10))
        assert _engagement_key(eng) == ("My Post", date(2025, 7, 10))


# ---------------------------------------------------------------------------
# fetch_engagement_flow tests (task 9.3 — clean state handling)
# ---------------------------------------------------------------------------


class TestFetchEngagementFlow:
    """Tests for the orchestrated engagement fetch flow.

    Validates: REQ-034.6, REQ-034.7, REQ-034.8, REQ-034.9
    """

    def test_clean_state_no_posts(self) -> None:
        """API returns empty list → clean state, no error, empty result."""
        api = StubDevToAPI(articles=[])
        memory = StubEngagementMemory()
        collector = ErrorCollector()

        result = fetch_engagement_flow(
            api, memory, collector, username="mike", api_key="key", run_date=RUN_DATE,
        )

        assert result == []
        assert not collector.has_errors
        assert memory.save_count == 0  # no store attempted

    def test_api_unreachable_logs_failure(self) -> None:
        """API raises exception → failure logged, empty result, no abort."""
        api = StubDevToAPI(error=ConnectionError("network down"))
        memory = StubEngagementMemory()
        collector = ErrorCollector()

        result = fetch_engagement_flow(
            api, memory, collector, username="mike", api_key="key", run_date=RUN_DATE,
        )

        assert result == []
        assert collector.has_errors
        assert len(collector.errors) == 1
        err = collector.errors[0]
        assert err.step == "engagement"
        assert err.target == "dev.to API"
        assert "network down" in err.error_message
        assert memory.save_count == 0

    def test_successful_fetch_stores_engagements(self) -> None:
        """API returns posts → engagements stored in memory."""
        api = StubDevToAPI(articles=[SAMPLE_ARTICLE])
        memory = StubEngagementMemory()
        collector = ErrorCollector()

        result = fetch_engagement_flow(
            api, memory, collector, username="mike", api_key="key", run_date=RUN_DATE,
        )

        assert len(result) == 1
        assert result[0].post_title == "Building Agents with Strands SDK"
        assert not collector.has_errors
        assert memory.save_count == 1

    def test_api_unreachable_does_not_raise(self) -> None:
        """API failure must not propagate — run continues."""
        api = StubDevToAPI(error=TimeoutError("timed out"))
        memory = StubEngagementMemory()
        collector = ErrorCollector()

        # Should not raise
        result = fetch_engagement_flow(
            api, memory, collector, username="mike", api_key="key", run_date=RUN_DATE,
        )
        assert result == []

    def test_error_context_records_username(self) -> None:
        """Failure log includes username for Agent_Log traceability."""
        api = StubDevToAPI(error=RuntimeError("500 server error"))
        memory = StubEngagementMemory()
        collector = ErrorCollector()

        fetch_engagement_flow(
            api, memory, collector, username="testuser", api_key="key", run_date=RUN_DATE,
        )

        err = collector.errors[0]
        assert err.context["username"] == "testuser"
        assert err.context["action"] == "fetch_engagement_metrics"
