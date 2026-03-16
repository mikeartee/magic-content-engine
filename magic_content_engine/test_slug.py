"""Tests for slug generation.

Requirements: REQ-028.1, REQ-028.2, REQ-028.3
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from magic_content_engine.models import Article
from magic_content_engine.slug import (
    _SLUG_REGEX,
    derive_topic,
    generate_slug,
    make_output_dirname,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIR_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+(-[a-z0-9]+)*$")


def _make_article(
    title: str = "Test Article",
    score: int | None = 4,
) -> Article:
    return Article(
        url="https://example.com",
        title=title,
        source="example.com",
        source_type="primary",
        discovered_date=date(2025, 7, 14),
        relevance_score=score,
    )


# ---------------------------------------------------------------------------
# generate_slug
# ---------------------------------------------------------------------------


class TestGenerateSlug:
    def test_normal_text(self):
        slug = generate_slug("AgentCore Browser Launch")
        assert slug == "agentcore-browser-launch"
        assert _SLUG_REGEX.match(slug)

    def test_strips_special_characters(self):
        slug = generate_slug("Hello, World! @2025")
        assert _SLUG_REGEX.match(slug)
        assert "@" not in slug
        assert "!" not in slug
        assert "," not in slug

    def test_collapses_consecutive_hyphens(self):
        slug = generate_slug("foo---bar")
        assert "--" not in slug
        assert slug == "foo-bar"

    def test_strips_leading_trailing_hyphens(self):
        slug = generate_slug("---hello---")
        assert slug == "hello"
        assert not slug.startswith("-")
        assert not slug.endswith("-")

    def test_empty_input_returns_content(self):
        assert generate_slug("") == "content"

    def test_only_special_chars_returns_content(self):
        assert generate_slug("!!!@@@###") == "content"

    def test_output_matches_regex(self):
        inputs = [
            "AgentCore Runtime",
            "strands-sdk-update",
            "Kiro IDE v2.0 Release!",
            "AWS re:Invent 2025",
            "  spaces  everywhere  ",
        ]
        for text in inputs:
            slug = generate_slug(text)
            assert _SLUG_REGEX.match(slug), f"Slug {slug!r} from {text!r} failed regex"


# ---------------------------------------------------------------------------
# derive_topic
# ---------------------------------------------------------------------------


class TestDeriveTopic:
    def test_uses_highest_scored_article_title(self):
        articles = [
            _make_article("Low Score Article", score=2),
            _make_article("High Score Article", score=5),
            _make_article("Mid Score Article", score=3),
        ]
        assert derive_topic(articles) == "High Score Article"

    def test_empty_list_returns_weekly_update(self):
        assert derive_topic([]) == "weekly-update"

    def test_single_article(self):
        articles = [_make_article("Only Article", score=4)]
        assert derive_topic(articles) == "Only Article"

    def test_none_scores_treated_as_zero(self):
        articles = [
            _make_article("No Score", score=None),
            _make_article("Has Score", score=3),
        ]
        assert derive_topic(articles) == "Has Score"


# ---------------------------------------------------------------------------
# make_output_dirname
# ---------------------------------------------------------------------------


class TestMakeOutputDirname:
    def test_correct_format(self):
        dirname = make_output_dirname(date(2025, 7, 14), "agentcore-browser-launch")
        assert dirname == "2025-07-14-agentcore-browser-launch"

    def test_matches_directory_regex(self):
        dirname = make_output_dirname(date(2025, 1, 5), "strands-sdk")
        assert _DIR_REGEX.match(dirname)

    def test_single_word_slug(self):
        dirname = make_output_dirname(date(2025, 12, 31), "content")
        assert dirname == "2025-12-31-content"
        assert _DIR_REGEX.match(dirname)
