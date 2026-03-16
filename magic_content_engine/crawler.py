"""Source crawler for the Magic Content Engine.

Crawls primary and secondary research sources via a browser
abstraction.  The actual AgentCore Browser integration is wired in by
the orchestrator; this module depends only on the ``BrowserProtocol``
interface.

Implements:
- BrowserProtocol — dependency-injection interface for the browser
- Keyword filtering for aws.amazon.com/new/
- 3-attempt retry with 2 s fixed delay per source (via retry_crawl)
- Log-and-continue: failures are recorded in ErrorCollector and the
  crawl proceeds to the next source

Requirements: REQ-002.1, REQ-002.2, REQ-002.3, REQ-003.1, REQ-003.2
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from magic_content_engine.errors import ErrorCollector, retry_crawl
from magic_content_engine.models import Article

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Browser abstraction
# ---------------------------------------------------------------------------


@dataclass
class CrawlResult:
    """Raw page data returned by the browser for a single URL."""

    url: str
    content: str  # page text / HTML body
    title: str = ""
    links: list[str] | None = None  # discovered article links on the page


@runtime_checkable
class BrowserProtocol(Protocol):
    """Dependency-injection interface for the crawling backend.

    In production this is backed by AgentCore Browser.  Tests can
    supply a lightweight stub.
    """

    def fetch_page(self, url: str) -> CrawlResult:
        """Navigate to *url* and return the page content.

        Raises on network / browser errors so that ``retry_crawl``
        can catch and retry.
        """
        ...


# ---------------------------------------------------------------------------
# Source definitions
# ---------------------------------------------------------------------------

AWS_NEWS_KEYWORDS: tuple[str, ...] = ("bedrock", "agentcore", "kiro", "lambda")

PRIMARY_SOURCES: list[dict[str, str]] = [
    {"url": "https://kiro.dev/changelog/ide/", "name": "kiro.dev/changelog/ide/"},
    {"url": "https://github.com/kirodotdev/Kiro/issues", "name": "github.com/kirodotdev/Kiro/issues"},
    {"url": "https://aws.amazon.com/new/", "name": "aws.amazon.com/new/"},
    {"url": "https://aws.amazon.com/blogs/machine-learning/", "name": "aws.amazon.com/blogs/machine-learning/"},
    {"url": "https://community.aws/", "name": "community.aws/"},
]

SECONDARY_SOURCES: list[dict[str, str]] = [
    {"url": "https://github.com/awslabs/", "name": "github.com/awslabs/"},
    {"url": "https://strandsagents.com", "name": "strandsagents.com"},
    {"url": "https://repost.aws/", "name": "repost.aws/"},
    {"url": "https://kiro.dev/blog/", "name": "kiro.dev/blog/"},
]


# ---------------------------------------------------------------------------
# Keyword filter (REQ-002.2)
# ---------------------------------------------------------------------------

def matches_aws_news_keywords(text: str) -> bool:
    """Return True if *text* contains at least one AWS news keyword.

    Matching is case-insensitive.
    """
    lower = text.lower()
    return any(kw in lower for kw in AWS_NEWS_KEYWORDS)


# ---------------------------------------------------------------------------
# Article extraction helpers
# ---------------------------------------------------------------------------

def _articles_from_crawl(
    result: CrawlResult,
    source_name: str,
    run_date: date,
    source_type: str = "primary",
) -> list[Article]:
    """Convert a CrawlResult into a list of discovered Articles.

    If the CrawlResult contains discrete links (e.g. a listing page),
    each link becomes a separate Article.  Otherwise the page itself
    is treated as a single article.
    """
    articles: list[Article] = []

    if result.links:
        for link in result.links:
            articles.append(
                Article(
                    url=link,
                    title="",  # title extracted later during metadata phase
                    source=source_name,
                    source_type=source_type,
                    discovered_date=run_date,
                )
            )
    else:
        # Treat the whole page as a single article entry
        articles.append(
            Article(
                url=result.url,
                title=result.title,
                source=source_name,
                source_type=source_type,
                discovered_date=run_date,
            )
        )

    return articles


# ---------------------------------------------------------------------------
# Primary source crawler (REQ-002.1, REQ-002.2, REQ-002.3)
# ---------------------------------------------------------------------------

def crawl_primary_sources(
    browser: BrowserProtocol,
    run_date: date,
    collector: ErrorCollector | None = None,
) -> list[Article]:
    """Crawl all five primary research sources and return discovered articles.

    Each source is attempted up to 3 times with a 2 s fixed delay
    between retries (delegated to ``retry_crawl``).  On final failure
    the error is recorded in *collector* and the crawl moves to the
    next source.

    For ``aws.amazon.com/new/`` the keyword filter (REQ-002.2) is
    applied: only articles whose content contains at least one of
    "bedrock", "agentcore", "kiro", or "lambda" (case-insensitive)
    are kept.
    """
    all_articles: list[Article] = []

    for source in PRIMARY_SOURCES:
        url = source["url"]
        name = source["name"]

        logger.info("Crawling primary source: %s", name)

        result: CrawlResult | None = retry_crawl(
            browser.fetch_page,
            url,
            collector=collector,
            target=name,
        )

        if result is None:
            # retry_crawl already logged the failure and added to collector
            logger.warning("Skipping source after retry exhaustion: %s", name)
            continue

        articles = _articles_from_crawl(result, name, run_date)

        # Apply keyword filter for aws.amazon.com/new/ (REQ-002.2)
        if name == "aws.amazon.com/new/":
            before_count = len(articles)
            if not result.links:
                # Single-page result: filter based on page content
                articles = [
                    a for a in articles
                    if matches_aws_news_keywords(result.content)
                ]
            # When the result contains individual links we cannot
            # inspect each article's content yet (only the listing
            # page has been fetched).  Those articles are kept here
            # and filtered per-article after individual page crawls
            # in a later pipeline stage.
            filtered_count = len(articles)
            logger.info(
                "AWS news keyword filter: %d -> %d articles",
                before_count,
                filtered_count,
            )

        all_articles.extend(articles)
        logger.info(
            "Discovered %d article(s) from %s", len(articles), name,
        )

    logger.info(
        "Primary crawl complete: %d total article(s) discovered",
        len(all_articles),
    )
    return all_articles


# ---------------------------------------------------------------------------
# Secondary source crawler (REQ-003.1, REQ-003.2)
# ---------------------------------------------------------------------------

def crawl_secondary_sources(
    browser: BrowserProtocol,
    run_date: date,
    collector: ErrorCollector | None = None,
) -> list[Article]:
    """Crawl all four secondary research sources and return discovered articles.

    Each source is attempted up to 3 times with a 2 s fixed delay
    between retries (delegated to ``retry_crawl``).  On final failure
    the error is recorded in *collector* and the crawl moves to the
    next source.

    Secondary sources do not require keyword filtering.  All
    discovered articles are tagged with ``source_type="secondary"``.
    """
    all_articles: list[Article] = []

    for source in SECONDARY_SOURCES:
        url = source["url"]
        name = source["name"]

        logger.info("Crawling secondary source: %s", name)

        result: CrawlResult | None = retry_crawl(
            browser.fetch_page,
            url,
            collector=collector,
            target=name,
        )

        if result is None:
            logger.warning("Skipping source after retry exhaustion: %s", name)
            continue

        articles = _articles_from_crawl(result, name, run_date, source_type="secondary")

        all_articles.extend(articles)
        logger.info(
            "Discovered %d article(s) from %s", len(articles), name,
        )

    logger.info(
        "Secondary crawl complete: %d total article(s) discovered",
        len(all_articles),
    )
    return all_articles
