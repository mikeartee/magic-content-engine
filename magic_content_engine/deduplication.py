"""Article deduplication against AgentCore Memory (long-term).

Checks each discovered article URL against previously processed URLs
stored in long-term memory.  Articles that have already been covered
are marked ``previously_covered`` and excluded from scoring.  After
user confirmation, confirmed article URLs and the run date are
persisted to long-term memory for future deduplication.

The actual AgentCore Memory integration is wired in by the
orchestrator; this module depends only on the ``MemoryProtocol``
interface so that tests can supply a lightweight stub.

Requirements: REQ-004.1, REQ-004.2, REQ-004.3
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Protocol, runtime_checkable

from magic_content_engine.models import Article

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Memory abstraction
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryProtocol(Protocol):
    """Dependency-injection interface for the long-term memory backend.

    In production this is backed by AgentCore Memory (long-term).
    Tests can supply a lightweight stub.
    """

    def is_url_previously_covered(self, url: str) -> bool:
        """Return ``True`` if *url* was stored in a previous run."""
        ...

    def store_article_url(self, url: str, run_date: date) -> None:
        """Persist *url* and *run_date* in long-term memory."""
        ...


# ---------------------------------------------------------------------------
# Deduplication (REQ-004.1, REQ-004.2)
# ---------------------------------------------------------------------------


def deduplicate_articles(
    articles: list[Article],
    memory: MemoryProtocol,
) -> list[Article]:
    """Filter out articles that have been covered in previous runs.

    For each article in *articles*, queries *memory* to check whether
    the URL has been processed before.  Matched articles have their
    ``status`` set to ``"previously_covered"`` and are excluded from
    the returned list.

    Returns only the articles that are new (not previously covered).
    """
    new_articles: list[Article] = []

    for article in articles:
        if memory.is_url_previously_covered(article.url):
            article.status = "previously_covered"
            logger.info(
                "Dedup: skipping previously covered article: %s",
                article.url,
            )
        else:
            new_articles.append(article)

    logger.info(
        "Deduplication complete: %d of %d articles are new",
        len(new_articles),
        len(articles),
    )
    return new_articles


# ---------------------------------------------------------------------------
# Store confirmed articles (REQ-004.3)
# ---------------------------------------------------------------------------


def store_confirmed_articles(
    articles: list[Article],
    run_date: date,
    memory: MemoryProtocol,
) -> None:
    """Persist confirmed article URLs and run date in long-term memory.

    Called after the user confirms the article list.  Only articles
    with ``status == "confirmed"`` are stored.
    """
    stored_count = 0
    for article in articles:
        if article.status == "confirmed":
            memory.store_article_url(article.url, run_date)
            stored_count += 1
            logger.debug(
                "Stored confirmed article URL: %s (run %s)",
                article.url,
                run_date,
            )

    logger.info(
        "Stored %d confirmed article URL(s) for run %s",
        stored_count,
        run_date,
    )
