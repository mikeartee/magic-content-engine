"""Tests for metadata extraction pipeline.

Requirements: REQ-006.1, REQ-006.2, REQ-006.3, REQ-027.2
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from magic_content_engine.errors import ErrorCollector
from magic_content_engine.metadata import (
    _apply_fallbacks,
    _parse_extraction_response,
    extract_metadata,
)
from magic_content_engine.models import Article, ArticleMetadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_article(url: str = "https://example.com/post", title: str = "Test Article") -> Article:
    return Article(
        url=url,
        title=title,
        source="example.com",
        source_type="primary",
        discovered_date=date(2025, 7, 14),
        relevance_score=4,
        status="scored",
    )


def _make_llm(response: str | Exception):
    """Return a fake LLM callable that returns *response* or raises it."""
    def fake_llm(prompt: str, model_id: str) -> str:
        if isinstance(response, Exception):
            raise response
        return response
    return fake_llm


# ---------------------------------------------------------------------------
# _parse_extraction_response
# ---------------------------------------------------------------------------


class TestParseExtractionResponse:
    def test_valid_full_response(self):
        raw = json.dumps({
            "title": "My Article",
            "publication_date": "2025-07-14",
            "author": "Jane Doe",
            "publisher": "AWS Blog",
            "canonical_url": "https://aws.amazon.com/blogs/my-article",
        })
        result = _parse_extraction_response(raw)
        assert result["title"] == "My Article"
        assert result["publication_date"] == "2025-07-14"
        assert result["author"] == "Jane Doe"
        assert result["publisher"] == "AWS Blog"
        assert result["canonical_url"] == "https://aws.amazon.com/blogs/my-article"

    def test_null_fields(self):
        raw = json.dumps({
            "title": "Title Only",
            "publication_date": None,
            "author": None,
            "publisher": None,
            "canonical_url": None,
        })
        result = _parse_extraction_response(raw)
        assert result["title"] == "Title Only"
        assert result["author"] is None
        assert result["publisher"] is None

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            _parse_extraction_response("not json at all")

    def test_non_object_raises(self):
        with pytest.raises(ValueError, match="Expected a JSON object"):
            _parse_extraction_response("[1, 2, 3]")


# ---------------------------------------------------------------------------
# _apply_fallbacks
# ---------------------------------------------------------------------------


class TestApplyFallbacks:
    def test_all_fields_present(self):
        article = _make_article()
        metadata = {
            "title": "Extracted Title",
            "publication_date": "2025-07-14",
            "author": "Jane Doe",
            "publisher": "AWS Blog",
            "canonical_url": "https://example.com/canonical",
        }
        result = _apply_fallbacks(metadata, article)
        assert result.title == "Extracted Title"
        assert result.author == "Jane Doe"
        assert result.publisher == "AWS Blog"
        assert result.article_url == article.url

    def test_missing_author_uses_fallback(self):
        article = _make_article()
        metadata = {"title": "T", "publication_date": None, "author": None, "publisher": "P", "canonical_url": None}
        result = _apply_fallbacks(metadata, article)
        assert result.author == "Amazon Web Services"

    def test_empty_author_uses_fallback(self):
        article = _make_article()
        metadata = {"title": "T", "publication_date": None, "author": "", "publisher": "P", "canonical_url": None}
        result = _apply_fallbacks(metadata, article)
        assert result.author == "Amazon Web Services"

    def test_whitespace_author_uses_fallback(self):
        article = _make_article()
        metadata = {"title": "T", "publication_date": None, "author": "   ", "publisher": "P", "canonical_url": None}
        result = _apply_fallbacks(metadata, article)
        assert result.author == "Amazon Web Services"

    def test_missing_publisher_uses_fallback(self):
        article = _make_article()
        metadata = {"title": "T", "publication_date": None, "author": "A", "publisher": None, "canonical_url": None}
        result = _apply_fallbacks(metadata, article)
        assert result.publisher == "Amazon Web Services"

    def test_empty_publisher_uses_fallback(self):
        article = _make_article()
        metadata = {"title": "T", "publication_date": None, "author": "A", "publisher": "", "canonical_url": None}
        result = _apply_fallbacks(metadata, article)
        assert result.publisher == "Amazon Web Services"

    def test_missing_title_falls_back_to_article_title(self):
        article = _make_article(title="Discovered Title")
        metadata = {"title": None, "publication_date": None, "author": "A", "publisher": "P", "canonical_url": None}
        result = _apply_fallbacks(metadata, article)
        assert result.title == "Discovered Title"

    def test_empty_title_falls_back_to_article_title(self):
        article = _make_article(title="Discovered Title")
        metadata = {"title": "", "publication_date": None, "author": "A", "publisher": "P", "canonical_url": None}
        result = _apply_fallbacks(metadata, article)
        assert result.title == "Discovered Title"


# ---------------------------------------------------------------------------
# extract_metadata (integration)
# ---------------------------------------------------------------------------


class TestExtractMetadata:
    def test_successful_extraction(self):
        llm_response = json.dumps({
            "title": "AgentCore Launch",
            "publication_date": "2025-07-10",
            "author": "AWS Team",
            "publisher": "AWS Blog",
            "canonical_url": "https://aws.amazon.com/blogs/agentcore",
        })
        articles = [_make_article(url="https://aws.amazon.com/blogs/agentcore", title="AgentCore")]
        results = extract_metadata(articles, _make_llm(llm_response))

        assert len(results) == 1
        assert results[0].title == "AgentCore Launch"
        assert results[0].author == "AWS Team"
        assert results[0].publisher == "AWS Blog"
        assert results[0].article_url == "https://aws.amazon.com/blogs/agentcore"

    def test_extraction_failure_skips_article(self):
        collector = ErrorCollector()
        articles = [_make_article(url="https://fail.example.com")]
        results = extract_metadata(articles, _make_llm(RuntimeError("LLM down")), collector=collector)

        assert len(results) == 0
        assert collector.has_errors
        assert collector.errors[0].step == "extract"
        assert collector.errors[0].target == "https://fail.example.com"

    def test_partial_failure_continues(self):
        """First article fails, second succeeds — only second returned."""
        call_count = 0
        good_response = json.dumps({
            "title": "Good Article",
            "publication_date": None,
            "author": None,
            "publisher": None,
            "canonical_url": None,
        })

        def mixed_llm(prompt: str, model_id: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Transient failure")
            return good_response

        collector = ErrorCollector()
        articles = [
            _make_article(url="https://fail.example.com", title="Fail"),
            _make_article(url="https://good.example.com", title="Good"),
        ]
        results = extract_metadata(articles, mixed_llm, collector=collector)

        assert len(results) == 1
        assert results[0].article_url == "https://good.example.com"
        assert results[0].author == "Amazon Web Services"  # fallback
        assert results[0].publisher == "Amazon Web Services"  # fallback
        assert len(collector.errors) == 1

    def test_empty_article_list(self):
        results = extract_metadata([], _make_llm("{}"))
        assert results == []

    def test_fallbacks_applied_on_null_fields(self):
        llm_response = json.dumps({
            "title": "Some Title",
            "publication_date": None,
            "author": None,
            "publisher": None,
            "canonical_url": None,
        })
        articles = [_make_article()]
        results = extract_metadata(articles, _make_llm(llm_response))

        assert len(results) == 1
        assert results[0].author == "Amazon Web Services"
        assert results[0].publisher == "Amazon Web Services"
