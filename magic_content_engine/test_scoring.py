"""Tests for the relevance scoring pipeline.

Requirements: REQ-005.1, REQ-005.2, REQ-005.3, REQ-005.4, REQ-027.2
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from magic_content_engine.errors import ErrorCollector
from magic_content_engine.models import Article
from magic_content_engine.scoring import (
    _parse_score_response,
    score_articles,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_article(title: str = "Test Article", url: str = "https://example.com") -> Article:
    return Article(
        url=url,
        title=title,
        source="example.com",
        source_type="primary",
        discovered_date=date(2025, 7, 14),
    )


def _fake_llm(score: int, rationale: str = "test rationale"):
    """Return a callable that always responds with the given score."""
    def _call(prompt: str, model_id: str) -> str:
        return json.dumps({"score": score, "rationale": rationale})
    return _call


# ---------------------------------------------------------------------------
# _parse_score_response
# ---------------------------------------------------------------------------


class TestParseScoreResponse:
    def test_valid_json(self):
        score, rationale = _parse_score_response('{"score": 4, "rationale": "relevant"}')
        assert score == 4
        assert rationale == "relevant"

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            _parse_score_response("not json at all")

    def test_score_out_of_range_raises(self):
        with pytest.raises(ValueError, match="integer in \\[1, 5\\]"):
            _parse_score_response('{"score": 6, "rationale": "oops"}')

    def test_score_zero_raises(self):
        with pytest.raises(ValueError, match="integer in \\[1, 5\\]"):
            _parse_score_response('{"score": 0, "rationale": "oops"}')

    def test_score_not_int_raises(self):
        with pytest.raises(ValueError, match="integer in \\[1, 5\\]"):
            _parse_score_response('{"score": 3.5, "rationale": "oops"}')

    def test_missing_rationale_defaults_empty(self):
        score, rationale = _parse_score_response('{"score": 2}')
        assert score == 2
        assert rationale == ""


# ---------------------------------------------------------------------------
# score_articles — threshold filtering
# ---------------------------------------------------------------------------


class TestScoreArticles:
    def test_articles_above_threshold_returned(self):
        articles = [_make_article(url=f"https://example.com/{i}") for i in range(3)]
        result = score_articles(articles, llm=_fake_llm(4), threshold=3)
        assert len(result) == 3
        for a in result:
            assert a.status == "scored"
            assert a.relevance_score == 4

    def test_articles_below_threshold_excluded(self):
        articles = [_make_article()]
        result = score_articles(articles, llm=_fake_llm(2), threshold=3)
        assert len(result) == 0
        assert articles[0].status == "excluded"
        assert articles[0].relevance_score == 2

    def test_articles_at_threshold_kept(self):
        articles = [_make_article()]
        result = score_articles(articles, llm=_fake_llm(3), threshold=3)
        assert len(result) == 1
        assert articles[0].status == "scored"

    def test_mixed_scores_filters_correctly(self):
        articles = [_make_article(url=f"https://example.com/{i}") for i in range(4)]
        scores = [5, 2, 3, 1]
        call_count = 0

        def _varying_llm(prompt: str, model_id: str) -> str:
            nonlocal call_count
            s = scores[call_count]
            call_count += 1
            return json.dumps({"score": s, "rationale": f"score {s}"})

        result = score_articles(articles, llm=_varying_llm, threshold=3)
        assert len(result) == 2
        assert result[0].relevance_score == 5
        assert result[1].relevance_score == 3

    def test_rationale_recorded(self):
        articles = [_make_article()]
        score_articles(articles, llm=_fake_llm(4, "very relevant"), threshold=3)
        assert articles[0].scoring_rationale == "very relevant"

    def test_default_threshold_from_config(self):
        """score_articles uses config.RELEVANCE_THRESHOLD when threshold is None."""
        articles = [_make_article()]
        # config default is 3; score of 3 should pass
        result = score_articles(articles, llm=_fake_llm(3))
        assert len(result) == 1


# ---------------------------------------------------------------------------
# score_articles — error handling (REQ-027.2)
# ---------------------------------------------------------------------------


class TestScoreArticlesErrorHandling:
    def test_llm_failure_skips_article_continues(self):
        articles = [
            _make_article(url="https://example.com/fail"),
            _make_article(url="https://example.com/ok"),
        ]
        call_count = 0

        def _failing_then_ok(prompt: str, model_id: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("LLM timeout")
            return json.dumps({"score": 4, "rationale": "good"})

        collector = ErrorCollector()
        result = score_articles(articles, llm=_failing_then_ok, collector=collector, threshold=3)

        assert len(result) == 1
        assert result[0].url == "https://example.com/ok"
        assert collector.has_errors
        assert len(collector.errors) == 1
        assert collector.errors[0].step == "score"
        assert collector.errors[0].target == "https://example.com/fail"

    def test_invalid_json_response_skips_article(self):
        articles = [_make_article()]

        def _bad_json(prompt: str, model_id: str) -> str:
            return "not json"

        collector = ErrorCollector()
        result = score_articles(articles, llm=_bad_json, collector=collector, threshold=3)

        assert len(result) == 0
        assert collector.has_errors

    def test_empty_article_list_returns_empty(self):
        result = score_articles([], llm=_fake_llm(5), threshold=3)
        assert result == []

    def test_all_failures_returns_empty(self):
        articles = [_make_article(url=f"https://example.com/{i}") for i in range(3)]

        def _always_fail(prompt: str, model_id: str) -> str:
            raise RuntimeError("boom")

        collector = ErrorCollector()
        result = score_articles(articles, llm=_always_fail, collector=collector, threshold=3)

        assert result == []
        assert len(collector.errors) == 3


# ---------------------------------------------------------------------------
# Engagement-weighted scoring (REQ-034.3, REQ-034.10)
# ---------------------------------------------------------------------------


class TestEngagementWeightedScoring:
    """Tests for engagement-weighted scoring in score_articles."""

    def _make_engagement(
        self, title: str = "AgentCore Runtime Deep Dive", views: int = 500, reactions: int = 50,
    ) -> "PostEngagement":
        from magic_content_engine.models import PostEngagement
        return PostEngagement(
            post_title=title,
            publication_date=date(2025, 7, 7),
            url="https://dev.to/test/post",
            views=views,
            reactions=reactions,
        )

    def test_no_metrics_no_boost(self):
        """When engagement_metrics is None, scoring is identical to baseline (REQ-034.10)."""
        articles = [_make_article(title="AgentCore Runtime Update")]
        result = score_articles(articles, llm=_fake_llm(3), threshold=3, engagement_metrics=None)
        assert len(result) == 1
        assert result[0].relevance_score == 3
        assert "engagement boost" not in (result[0].scoring_rationale or "")

    def test_empty_metrics_no_boost(self):
        """When engagement_metrics is empty list, scoring is identical to baseline (REQ-034.10)."""
        articles = [_make_article(title="AgentCore Runtime Update")]
        result = score_articles(articles, llm=_fake_llm(3), threshold=3, engagement_metrics=[])
        assert len(result) == 1
        assert result[0].relevance_score == 3
        assert "engagement boost" not in (result[0].scoring_rationale or "")

    def test_matching_topic_gets_boost(self):
        """Article matching a high-engagement topic keyword gets a score boost (REQ-034.3)."""
        metrics = [self._make_engagement("AgentCore Runtime Deep Dive", views=500, reactions=50)]
        articles = [_make_article(title="AgentCore Runtime Update")]
        result = score_articles(articles, llm=_fake_llm(3), threshold=3, engagement_metrics=metrics)
        assert len(result) == 1
        assert result[0].relevance_score == 4  # 3 + 1 boost
        assert "engagement boost" in result[0].scoring_rationale

    def test_no_matching_topic_no_boost(self):
        """Article not matching any engagement keyword gets no boost."""
        metrics = [self._make_engagement("AgentCore Runtime Deep Dive", views=500, reactions=50)]
        articles = [_make_article(title="Unrelated Lambda News")]
        result = score_articles(articles, llm=_fake_llm(3), threshold=3, engagement_metrics=metrics)
        assert len(result) == 1
        assert result[0].relevance_score == 3
        assert "engagement boost" not in (result[0].scoring_rationale or "")

    def test_boost_capped_at_five(self):
        """Engagement boost cannot push score above 5."""
        metrics = [self._make_engagement("Kiro IDE Features", views=1000, reactions=200)]
        articles = [_make_article(title="Kiro IDE Changelog")]
        result = score_articles(articles, llm=_fake_llm(5), threshold=3, engagement_metrics=metrics)
        assert len(result) == 1
        assert result[0].relevance_score == 5  # capped, not 6

    def test_boost_can_push_article_above_threshold(self):
        """An article at score 2 with boost becomes 3 and passes threshold."""
        metrics = [self._make_engagement("Strands SDK Release", views=300, reactions=40)]
        articles = [_make_article(title="Strands SDK Update")]
        result = score_articles(articles, llm=_fake_llm(2), threshold=3, engagement_metrics=metrics)
        assert len(result) == 1
        assert result[0].relevance_score == 3
        assert result[0].status == "scored"
