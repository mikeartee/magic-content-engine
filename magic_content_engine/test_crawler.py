"""Tests for the primary source crawler.

Covers:
- All 5 primary sources are crawled (REQ-002.1)
- Keyword filter on aws.amazon.com/new/ (REQ-002.2)
- Retry after 3 failures logs error and continues (REQ-002.3)
- BrowserProtocol contract
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from magic_content_engine.crawler import (
    AWS_NEWS_KEYWORDS,
    BrowserProtocol,
    CrawlResult,
    PRIMARY_SOURCES,
    _articles_from_crawl,
    crawl_primary_sources,
    matches_aws_news_keywords,
)
from magic_content_engine.errors import ErrorCollector


# ---------------------------------------------------------------------------
# Stub browser
# ---------------------------------------------------------------------------

class StubBrowser:
    """A minimal browser that returns canned CrawlResults."""

    def __init__(
        self,
        responses: dict[str, CrawlResult] | None = None,
        fail_urls: set[str] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.fail_urls = fail_urls or set()
        self.fetched_urls: list[str] = []

    def fetch_page(self, url: str) -> CrawlResult:
        self.fetched_urls.append(url)
        if url in self.fail_urls:
            raise ConnectionError(f"Simulated failure for {url}")
        if url in self.responses:
            return self.responses[url]
        return CrawlResult(url=url, content="default content", title="Default")



# Verify the stub satisfies the protocol
assert isinstance(StubBrowser(), BrowserProtocol)

RUN_DATE = date(2025, 7, 14)


# ---------------------------------------------------------------------------
# Keyword filter tests (REQ-002.2)
# ---------------------------------------------------------------------------

class TestMatchesAwsNewsKeywords:
    def test_matches_keyword_lowercase(self) -> None:
        assert matches_aws_news_keywords("New bedrock feature released")

    def test_matches_keyword_uppercase(self) -> None:
        assert matches_aws_news_keywords("AGENTCORE update available")

    def test_matches_keyword_mixed_case(self) -> None:
        assert matches_aws_news_keywords("Kiro IDE changelog")

    def test_matches_lambda(self) -> None:
        assert matches_aws_news_keywords("AWS Lambda now supports Python 3.13")

    def test_no_match(self) -> None:
        assert not matches_aws_news_keywords("Amazon S3 pricing update")

    def test_empty_string(self) -> None:
        assert not matches_aws_news_keywords("")


# ---------------------------------------------------------------------------
# Primary source crawl tests (REQ-002.1)
# ---------------------------------------------------------------------------

class TestCrawlPrimarySources:
    def test_crawls_all_five_sources(self) -> None:
        """All 5 primary source URLs are fetched."""
        browser = StubBrowser()
        crawl_primary_sources(browser, RUN_DATE)

        expected_urls = {s["url"] for s in PRIMARY_SOURCES}
        assert set(browser.fetched_urls) == expected_urls

    def test_returns_articles_from_all_sources(self) -> None:
        browser = StubBrowser()
        articles = crawl_primary_sources(browser, RUN_DATE)
        sources_seen = {a.source for a in articles}
        # aws.amazon.com/new/ may be filtered out if content has no keywords
        # so we check the other 4 are always present
        assert "kiro.dev/changelog/ide/" in sources_seen
        assert "github.com/kirodotdev/Kiro/issues" in sources_seen
        assert "aws.amazon.com/blogs/machine-learning/" in sources_seen
        assert "community.aws/" in sources_seen

    def test_all_articles_marked_primary(self) -> None:
        browser = StubBrowser()
        articles = crawl_primary_sources(browser, RUN_DATE)
        for a in articles:
            assert a.source_type == "primary"

    def test_all_articles_have_run_date(self) -> None:
        browser = StubBrowser()
        articles = crawl_primary_sources(browser, RUN_DATE)
        for a in articles:
            assert a.discovered_date == RUN_DATE


# ---------------------------------------------------------------------------
# AWS news keyword filtering during crawl (REQ-002.2)
# ---------------------------------------------------------------------------

class TestAwsNewsFiltering:
    def test_aws_news_with_keyword_kept(self) -> None:
        """aws.amazon.com/new/ page with a keyword passes the filter."""
        browser = StubBrowser(responses={
            "https://aws.amazon.com/new/": CrawlResult(
                url="https://aws.amazon.com/new/",
                content="Amazon Bedrock adds new model support",
                title="AWS News",
            ),
        })
        articles = crawl_primary_sources(browser, RUN_DATE)
        aws_articles = [a for a in articles if a.source == "aws.amazon.com/new/"]
        assert len(aws_articles) == 1

    def test_aws_news_without_keyword_filtered(self) -> None:
        """aws.amazon.com/new/ page without keywords is dropped."""
        browser = StubBrowser(responses={
            "https://aws.amazon.com/new/": CrawlResult(
                url="https://aws.amazon.com/new/",
                content="Amazon RDS pricing update for PostgreSQL",
                title="AWS News",
            ),
        })
        articles = crawl_primary_sources(browser, RUN_DATE)
        aws_articles = [a for a in articles if a.source == "aws.amazon.com/new/"]
        assert len(aws_articles) == 0

    def test_aws_news_with_links_kept_for_later_filtering(self) -> None:
        """When aws.amazon.com/new/ returns links, all are kept for later filtering."""
        browser = StubBrowser(responses={
            "https://aws.amazon.com/new/": CrawlResult(
                url="https://aws.amazon.com/new/",
                content="listing page",
                title="AWS News",
                links=[
                    "https://aws.amazon.com/new/article-1",
                    "https://aws.amazon.com/new/article-2",
                ],
            ),
        })
        articles = crawl_primary_sources(browser, RUN_DATE)
        aws_articles = [a for a in articles if a.source == "aws.amazon.com/new/"]
        # Links are kept; per-article filtering happens later
        assert len(aws_articles) == 2


# ---------------------------------------------------------------------------
# Retry and error handling (REQ-002.3)
# ---------------------------------------------------------------------------

class TestCrawlRetryAndContinue:
    @patch("magic_content_engine.crawler.retry_crawl")
    def test_failure_logged_and_continues(self, mock_retry) -> None:
        """When a source fails after retries, the error is logged and remaining sources are crawled."""
        # Make retry_crawl return None for the first source, CrawlResult for others
        def side_effect(fn, url, **kwargs):
            if url == "https://kiro.dev/changelog/ide/":
                return None
            return CrawlResult(url=url, content="content", title="Title")

        mock_retry.side_effect = side_effect
        collector = ErrorCollector()
        browser = StubBrowser()

        articles = crawl_primary_sources(browser, RUN_DATE, collector=collector)

        # Should still have articles from the other 4 sources
        # (aws.amazon.com/new/ may be filtered, so at least 3)
        sources_seen = {a.source for a in articles}
        assert "kiro.dev/changelog/ide/" not in sources_seen
        assert len(sources_seen) >= 3

    def test_collector_records_failure(self) -> None:
        """ErrorCollector captures the crawl failure."""
        browser = StubBrowser(fail_urls={
            "https://kiro.dev/changelog/ide/",
        })
        collector = ErrorCollector()

        # Use real retry_crawl but patch sleep to avoid delays
        with patch("magic_content_engine.errors.time.sleep"):
            articles = crawl_primary_sources(browser, RUN_DATE, collector=collector)

        assert collector.has_errors
        error = collector.errors[0]
        assert error.step == "crawl"
        assert "kiro.dev/changelog/ide/" in error.target

        # Other sources still produced articles
        sources_seen = {a.source for a in articles}
        assert "github.com/kirodotdev/Kiro/issues" in sources_seen


# ---------------------------------------------------------------------------
# Article extraction helper
# ---------------------------------------------------------------------------

class TestArticlesFromCrawl:
    def test_single_page_result(self) -> None:
        result = CrawlResult(url="https://example.com", content="body", title="Example")
        articles = _articles_from_crawl(result, "example.com", RUN_DATE)
        assert len(articles) == 1
        assert articles[0].url == "https://example.com"
        assert articles[0].title == "Example"

    def test_links_result(self) -> None:
        result = CrawlResult(
            url="https://example.com",
            content="listing",
            title="Listing",
            links=["https://example.com/a", "https://example.com/b"],
        )
        articles = _articles_from_crawl(result, "example.com", RUN_DATE)
        assert len(articles) == 2
        assert articles[0].url == "https://example.com/a"
        assert articles[1].url == "https://example.com/b"
