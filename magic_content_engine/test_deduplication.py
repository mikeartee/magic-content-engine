"""Unit tests for article deduplication."""

from __future__ import annotations

from datetime import date

from magic_content_engine.deduplication import (
    MemoryProtocol,
    deduplicate_articles,
    store_confirmed_articles,
)
from magic_content_engine.models import Article


# ---------------------------------------------------------------------------
# Lightweight memory stub
# ---------------------------------------------------------------------------


class StubMemory:
    """In-memory stub implementing MemoryProtocol."""

    def __init__(self, known_urls: set[str] | None = None) -> None:
        self._known: set[str] = known_urls or set()
        self.stored: list[tuple[str, date]] = []

    def is_url_previously_covered(self, url: str) -> bool:
        return url in self._known

    def store_article_url(self, url: str, run_date: date) -> None:
        self._known.add(url)
        self.stored.append((url, run_date))


def _make_article(url: str, status: str = "discovered") -> Article:
    return Article(
        url=url,
        title=f"Title for {url}",
        source="test-source",
        source_type="primary",
        discovered_date=date(2025, 7, 14),
        status=status,
    )


# ---------------------------------------------------------------------------
# Tests — deduplicate_articles
# ---------------------------------------------------------------------------


class TestDeduplicateArticles:
    def test_all_new_articles_pass_through(self) -> None:
        memory = StubMemory()
        articles = [_make_article("https://a.com"), _make_article("https://b.com")]

        result = deduplicate_articles(articles, memory)

        assert len(result) == 2
        assert all(a.status == "discovered" for a in result)

    def test_previously_covered_articles_excluded(self) -> None:
        memory = StubMemory(known_urls={"https://old.com"})
        articles = [
            _make_article("https://old.com"),
            _make_article("https://new.com"),
        ]

        result = deduplicate_articles(articles, memory)

        assert len(result) == 1
        assert result[0].url == "https://new.com"

    def test_previously_covered_status_set(self) -> None:
        memory = StubMemory(known_urls={"https://old.com"})
        old_article = _make_article("https://old.com")
        deduplicate_articles([old_article], memory)

        assert old_article.status == "previously_covered"

    def test_empty_article_list(self) -> None:
        memory = StubMemory(known_urls={"https://old.com"})
        result = deduplicate_articles([], memory)
        assert result == []

    def test_all_articles_previously_covered(self) -> None:
        memory = StubMemory(known_urls={"https://a.com", "https://b.com"})
        articles = [_make_article("https://a.com"), _make_article("https://b.com")]

        result = deduplicate_articles(articles, memory)

        assert result == []
        assert all(a.status == "previously_covered" for a in articles)

    def test_stub_satisfies_protocol(self) -> None:
        assert isinstance(StubMemory(), MemoryProtocol)


# ---------------------------------------------------------------------------
# Tests — store_confirmed_articles
# ---------------------------------------------------------------------------


class TestStoreConfirmedArticles:
    def test_stores_only_confirmed_articles(self) -> None:
        memory = StubMemory()
        articles = [
            _make_article("https://confirmed.com", status="confirmed"),
            _make_article("https://excluded.com", status="excluded"),
            _make_article("https://discovered.com", status="discovered"),
        ]
        run = date(2025, 7, 14)

        store_confirmed_articles(articles, run, memory)

        assert len(memory.stored) == 1
        assert memory.stored[0] == ("https://confirmed.com", run)

    def test_stores_multiple_confirmed(self) -> None:
        memory = StubMemory()
        articles = [
            _make_article("https://a.com", status="confirmed"),
            _make_article("https://b.com", status="confirmed"),
        ]
        run = date(2025, 7, 14)

        store_confirmed_articles(articles, run, memory)

        stored_urls = {url for url, _ in memory.stored}
        assert stored_urls == {"https://a.com", "https://b.com"}

    def test_no_confirmed_articles_stores_nothing(self) -> None:
        memory = StubMemory()
        articles = [_make_article("https://a.com", status="discovered")]

        store_confirmed_articles(articles, date(2025, 7, 14), memory)

        assert memory.stored == []

    def test_empty_list_stores_nothing(self) -> None:
        memory = StubMemory()
        store_confirmed_articles([], date(2025, 7, 14), memory)
        assert memory.stored == []
