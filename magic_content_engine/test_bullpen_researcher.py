"""Tests for the Researcher Lambda.

Covers:
- keyword filter correctness (aws.amazon.com/new/ filter)
- score threshold filter (only articles >= 3 pass)
- source failure handling (3 retries then skip)
- ResearchBrief JSON round-trip serialisation
- Property-based tests (Hypothesis):
  - score range invariant: relevance_score is int in [1, 5]
  - ResearchBrief JSON round-trip

Requirements: Bullpen Req 3, Req 4
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from magic_content_engine.bullpen.researcher import (
    ResearchBrief,
    ScoredArticle,
    _RawArticle,
    _parse_score_response,
    _retry_fetch,
    matches_aws_news_keywords,
    score_articles,
)
from magic_content_engine.errors import ErrorCollector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_raw(
    title: str = "Test Article",
    url: str = "https://example.com/article",
    source: str = "example.com",
    content: str = "",
) -> _RawArticle:
    return _RawArticle(title=title, url=url, source=source, content=content)


def _fake_llm(score: int, summary: str = "A test summary."):
    """Return a callable that always responds with the given score."""

    def _call(prompt: str, model_id: str) -> str:
        return json.dumps(
            {"score": score, "summary": summary, "rationale": "test rationale"}
        )

    return _call


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 1. Keyword filter correctness (aws.amazon.com/new/)
# ---------------------------------------------------------------------------


class TestMatchesAwsNewsKeywords:
    def test_matches_bedrock_lowercase(self) -> None:
        assert matches_aws_news_keywords("Amazon bedrock adds new model")

    def test_matches_agentcore_uppercase(self) -> None:
        assert matches_aws_news_keywords("AGENTCORE update available")

    def test_matches_kiro_mixed_case(self) -> None:
        assert matches_aws_news_keywords("Kiro IDE changelog update")

    def test_matches_lambda(self) -> None:
        assert matches_aws_news_keywords("AWS Lambda now supports Python 3.13")

    def test_no_match_returns_false(self) -> None:
        assert not matches_aws_news_keywords("Amazon RDS pricing update for PostgreSQL")

    def test_empty_string_returns_false(self) -> None:
        assert not matches_aws_news_keywords("")

    def test_partial_word_match(self) -> None:
        # "lambda" appears inside "lambdafunction" — still matches
        assert matches_aws_news_keywords("lambdafunction update")

    def test_all_keywords_match(self) -> None:
        for kw in ("bedrock", "agentcore", "kiro", "lambda"):
            assert matches_aws_news_keywords(kw), f"Expected match for keyword: {kw}"


# ---------------------------------------------------------------------------
# 2. Score threshold filter
# ---------------------------------------------------------------------------


class TestScoreThresholdFilter:
    def test_article_at_threshold_kept(self) -> None:
        articles = [_make_raw()]
        result = score_articles(articles, llm=_fake_llm(3), threshold=3)
        assert len(result) == 1
        assert result[0].relevance_score == 3

    def test_article_above_threshold_kept(self) -> None:
        articles = [_make_raw()]
        result = score_articles(articles, llm=_fake_llm(5), threshold=3)
        assert len(result) == 1
        assert result[0].relevance_score == 5

    def test_article_below_threshold_dropped(self) -> None:
        articles = [_make_raw()]
        result = score_articles(articles, llm=_fake_llm(2), threshold=3)
        assert len(result) == 0

    def test_score_1_dropped(self) -> None:
        articles = [_make_raw()]
        result = score_articles(articles, llm=_fake_llm(1), threshold=3)
        assert len(result) == 0

    def test_mixed_scores_only_passing_returned(self) -> None:
        articles = [
            _make_raw(url="https://example.com/a"),
            _make_raw(url="https://example.com/b"),
            _make_raw(url="https://example.com/c"),
            _make_raw(url="https://example.com/d"),
        ]
        scores = [5, 2, 3, 1]
        call_count = 0

        def _varying_llm(prompt: str, model_id: str) -> str:
            nonlocal call_count
            s = scores[call_count]
            call_count += 1
            return json.dumps({"score": s, "summary": f"summary {s}", "rationale": "r"})

        result = score_articles(articles, llm=_varying_llm, threshold=3)
        assert len(result) == 2
        assert result[0].relevance_score == 5
        assert result[1].relevance_score == 3

    def test_empty_article_list_returns_empty(self) -> None:
        result = score_articles([], llm=_fake_llm(5), threshold=3)
        assert result == []

    def test_scored_article_has_correct_fields(self) -> None:
        articles = [_make_raw(title="Kiro IDE Update", url="https://kiro.dev/changelog")]
        result = score_articles(articles, llm=_fake_llm(4, "Kiro IDE changelog."), threshold=3)
        assert len(result) == 1
        a = result[0]
        assert a.title == "Kiro IDE Update"
        assert a.url == "https://kiro.dev/changelog"
        assert a.source == "example.com"
        assert a.relevance_score == 4
        assert a.summary == "Kiro IDE changelog."

    def test_threshold_4_drops_score_3(self) -> None:
        articles = [_make_raw()]
        result = score_articles(articles, llm=_fake_llm(3), threshold=4)
        assert len(result) == 0

    def test_threshold_1_keeps_all(self) -> None:
        articles = [_make_raw(url=f"https://example.com/{i}") for i in range(3)]
        result = score_articles(articles, llm=_fake_llm(1), threshold=1)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# 3. Source failure handling (3 retries then skip)
# ---------------------------------------------------------------------------


class TestSourceFailureHandling:
    def test_retry_fetch_succeeds_on_first_attempt(self) -> None:
        sources_crawled: list[str] = []
        sources_failed: list[str] = []
        collector = ErrorCollector()
        call_count = 0

        def _fetch() -> list[_RawArticle]:
            nonlocal call_count
            call_count += 1
            return [_make_raw()]

        result = _retry_fetch(
            _fetch, "test-source", sources_crawled, sources_failed, collector
        )
        assert len(result) == 1
        assert call_count == 1
        assert "test-source" in sources_crawled
        assert "test-source" not in sources_failed
        assert not collector.has_errors

    def test_retry_fetch_succeeds_on_second_attempt(self) -> None:
        sources_crawled: list[str] = []
        sources_failed: list[str] = []
        collector = ErrorCollector()
        call_count = 0

        def _fetch() -> list[_RawArticle]:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("transient failure")
            return [_make_raw()]

        with patch("magic_content_engine.bullpen.researcher.time.sleep"):
            result = _retry_fetch(
                _fetch, "test-source", sources_crawled, sources_failed, collector
            )

        assert len(result) == 1
        assert call_count == 2
        assert "test-source" in sources_crawled
        assert not collector.has_errors

    def test_retry_fetch_exhausts_3_attempts_then_skips(self) -> None:
        sources_crawled: list[str] = []
        sources_failed: list[str] = []
        collector = ErrorCollector()
        call_count = 0

        def _fetch() -> list[_RawArticle]:
            nonlocal call_count
            call_count += 1
            raise ConnectionError("always fails")

        with patch("magic_content_engine.bullpen.researcher.time.sleep"):
            result = _retry_fetch(
                _fetch, "failing-source", sources_crawled, sources_failed, collector
            )

        assert result == []
        assert call_count == 3  # exactly 3 attempts
        assert "failing-source" not in sources_crawled
        assert "failing-source" in sources_failed
        assert collector.has_errors
        assert len(collector.errors) == 1
        assert collector.errors[0].step == "crawl"
        assert collector.errors[0].target == "failing-source"

    def test_retry_fetch_sleeps_between_attempts(self) -> None:
        sources_crawled: list[str] = []
        sources_failed: list[str] = []
        collector = ErrorCollector()

        def _fetch() -> list[_RawArticle]:
            raise ConnectionError("always fails")

        with patch("magic_content_engine.bullpen.researcher.time.sleep") as mock_sleep:
            _retry_fetch(
                _fetch, "failing-source", sources_crawled, sources_failed, collector
            )

        # Should sleep between attempt 1→2 and 2→3 (not after final failure)
        assert mock_sleep.call_count == 2
        for c in mock_sleep.call_args_list:
            assert c == call(2.0)

    def test_multiple_source_failures_all_recorded(self) -> None:
        sources_crawled: list[str] = []
        sources_failed: list[str] = []
        collector = ErrorCollector()

        def _always_fail() -> list[_RawArticle]:
            raise RuntimeError("boom")

        with patch("magic_content_engine.bullpen.researcher.time.sleep"):
            for name in ("source-a", "source-b"):
                _retry_fetch(
                    _always_fail, name, sources_crawled, sources_failed, collector
                )

        assert sources_failed == ["source-a", "source-b"]
        assert len(collector.errors) == 2

    def test_scoring_failure_skips_article_continues(self) -> None:
        articles = [
            _make_raw(url="https://example.com/fail"),
            _make_raw(url="https://example.com/ok"),
        ]
        call_count = 0

        def _failing_then_ok(prompt: str, model_id: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("LLM timeout")
            return json.dumps({"score": 4, "summary": "good", "rationale": "r"})

        collector = ErrorCollector()
        result = score_articles(articles, llm=_failing_then_ok, collector=collector, threshold=3)

        assert len(result) == 1
        assert result[0].url == "https://example.com/ok"
        assert collector.has_errors
        assert collector.errors[0].step == "score"
        assert collector.errors[0].target == "https://example.com/fail"

    def test_invalid_llm_json_skips_article(self) -> None:
        articles = [_make_raw()]

        def _bad_json(prompt: str, model_id: str) -> str:
            return "not json at all"

        collector = ErrorCollector()
        result = score_articles(articles, llm=_bad_json, collector=collector, threshold=3)
        assert len(result) == 0
        assert collector.has_errors


# ---------------------------------------------------------------------------
# 4. ResearchBrief JSON round-trip serialisation
# ---------------------------------------------------------------------------


class TestResearchBriefJsonRoundTrip:
    def _make_brief(self) -> ResearchBrief:
        return ResearchBrief(
            articles=[
                ScoredArticle(
                    title="Kiro IDE 1.0",
                    url="https://kiro.dev/changelog/ide/",
                    source="kiro.dev/changelog/ide/",
                    relevance_score=5,
                    summary="Kiro IDE 1.0 released with new features.",
                ),
                ScoredArticle(
                    title="AgentCore GA",
                    url="https://aws.amazon.com/new/agentcore",
                    source="aws.amazon.com/new/",
                    relevance_score=4,
                    summary="AgentCore is now generally available.",
                ),
            ],
            sources_crawled=[
                "kiro.dev/changelog/ide/",
                "aws.amazon.com/new/",
                "community.aws",
            ],
            sources_failed=["strandsagents.com"],
            run_timestamp="2025-07-14T09:00:00+00:00",
        )

    def test_to_dict_is_json_serialisable(self) -> None:
        brief = self._make_brief()
        d = brief.to_dict()
        # Should not raise
        serialised = json.dumps(d)
        assert isinstance(serialised, str)

    def test_round_trip_produces_equivalent_object(self) -> None:
        brief = self._make_brief()
        d = brief.to_dict()
        serialised = json.dumps(d)
        restored = ResearchBrief.from_dict(json.loads(serialised))

        assert restored.run_timestamp == brief.run_timestamp
        assert restored.sources_crawled == brief.sources_crawled
        assert restored.sources_failed == brief.sources_failed
        assert len(restored.articles) == len(brief.articles)

        for orig, rest in zip(brief.articles, restored.articles):
            assert rest.title == orig.title
            assert rest.url == orig.url
            assert rest.source == orig.source
            assert rest.relevance_score == orig.relevance_score
            assert rest.summary == orig.summary

    def test_empty_brief_round_trips(self) -> None:
        brief = ResearchBrief(
            articles=[],
            sources_crawled=[],
            sources_failed=[],
            run_timestamp="2025-07-14T09:00:00+00:00",
        )
        restored = ResearchBrief.from_dict(json.loads(json.dumps(brief.to_dict())))
        assert restored.articles == []
        assert restored.sources_crawled == []
        assert restored.sources_failed == []

    def test_to_dict_articles_are_plain_dicts(self) -> None:
        brief = self._make_brief()
        d = brief.to_dict()
        for a in d["articles"]:
            assert isinstance(a, dict)
            assert "title" in a
            assert "url" in a
            assert "source" in a
            assert "relevance_score" in a
            assert "summary" in a

    def test_from_dict_preserves_all_fields(self) -> None:
        data = {
            "articles": [
                {
                    "title": "Test",
                    "url": "https://example.com",
                    "source": "example.com",
                    "relevance_score": 3,
                    "summary": "A test article.",
                }
            ],
            "sources_crawled": ["example.com"],
            "sources_failed": [],
            "run_timestamp": "2025-07-14T09:00:00+00:00",
        }
        brief = ResearchBrief.from_dict(data)
        assert brief.articles[0].title == "Test"
        assert brief.articles[0].relevance_score == 3
        assert brief.sources_crawled == ["example.com"]
        assert brief.sources_failed == []


# ---------------------------------------------------------------------------
# 5. _parse_score_response unit tests
# ---------------------------------------------------------------------------


class TestParseScoreResponse:
    def test_valid_response(self) -> None:
        score, summary = _parse_score_response(
            '{"score": 4, "summary": "Good article.", "rationale": "relevant"}'
        )
        assert score == 4
        assert summary == "Good article."

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            _parse_score_response("not json")

    def test_score_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError, match="integer in \\[1, 5\\]"):
            _parse_score_response('{"score": 6, "summary": "x", "rationale": "r"}')

    def test_score_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="integer in \\[1, 5\\]"):
            _parse_score_response('{"score": 0, "summary": "x", "rationale": "r"}')

    def test_score_float_raises(self) -> None:
        with pytest.raises(ValueError, match="integer in \\[1, 5\\]"):
            _parse_score_response('{"score": 3.5, "summary": "x", "rationale": "r"}')

    def test_missing_summary_defaults_empty(self) -> None:
        score, summary = _parse_score_response('{"score": 3, "rationale": "r"}')
        assert score == 3
        assert summary == ""

    def test_strips_markdown_code_fences(self) -> None:
        raw = '```json\n{"score": 4, "summary": "Good.", "rationale": "r"}\n```'
        score, summary = _parse_score_response(raw)
        assert score == 4


# ---------------------------------------------------------------------------
# Property-based tests (Hypothesis)
# ---------------------------------------------------------------------------


# Strategy for valid ScoredArticle
_scored_article_strategy = st.builds(
    ScoredArticle,
    title=st.text(min_size=1, max_size=200),
    url=st.from_regex(r"https://[a-z0-9\-]+\.[a-z]{2,6}/[a-z0-9\-/]*", fullmatch=True),
    source=st.text(min_size=1, max_size=100),
    relevance_score=st.integers(min_value=1, max_value=5),
    summary=st.text(max_size=500),
)

# Strategy for ResearchBrief
_research_brief_strategy = st.builds(
    ResearchBrief,
    articles=st.lists(_scored_article_strategy, max_size=20),
    sources_crawled=st.lists(st.text(min_size=1, max_size=100), max_size=10),
    sources_failed=st.lists(st.text(min_size=1, max_size=100), max_size=10),
    run_timestamp=st.just("2025-07-14T09:00:00+00:00"),
)


class TestPropertyBased:
    @given(score=st.integers(min_value=1, max_value=5))
    def test_score_range_invariant_valid_scores(self, score: int) -> None:
        """For any valid score in [1,5], a ScoredArticle holds it correctly."""
        article = ScoredArticle(
            title="Test",
            url="https://example.com",
            source="example.com",
            relevance_score=score,
            summary="summary",
        )
        assert isinstance(article.relevance_score, int)
        assert 1 <= article.relevance_score <= 5

    @given(
        articles=st.lists(_scored_article_strategy, max_size=10),
        threshold=st.integers(min_value=1, max_value=5),
    )
    def test_score_threshold_invariant(
        self, articles: list[ScoredArticle], threshold: int
    ) -> None:
        """All articles returned by score_articles have relevance_score >= threshold."""
        # Build raw articles from scored ones (reuse title/url/source)
        raw = [
            _RawArticle(title=a.title, url=a.url, source=a.source)
            for a in articles
        ]

        # Build an LLM that returns the pre-assigned scores in order
        scores = [a.relevance_score for a in articles]
        call_idx = 0

        def _deterministic_llm(prompt: str, model_id: str) -> str:
            nonlocal call_idx
            s = scores[call_idx] if call_idx < len(scores) else 1
            call_idx += 1
            return json.dumps({"score": s, "summary": "s", "rationale": "r"})

        result = score_articles(raw, llm=_deterministic_llm, threshold=threshold)

        for a in result:
            assert isinstance(a.relevance_score, int)
            assert 1 <= a.relevance_score <= 5
            assert a.relevance_score >= threshold

    @given(brief=_research_brief_strategy)
    @settings(max_examples=50)
    def test_research_brief_json_round_trip(self, brief: ResearchBrief) -> None:
        """For any ResearchBrief, serialise → JSON → deserialise produces an equivalent object."""
        d = brief.to_dict()
        serialised = json.dumps(d)
        restored = ResearchBrief.from_dict(json.loads(serialised))

        assert restored.run_timestamp == brief.run_timestamp
        assert restored.sources_crawled == brief.sources_crawled
        assert restored.sources_failed == brief.sources_failed
        assert len(restored.articles) == len(brief.articles)

        for orig, rest in zip(brief.articles, restored.articles):
            assert rest.title == orig.title
            assert rest.url == orig.url
            assert rest.source == orig.source
            assert rest.relevance_score == orig.relevance_score
            assert rest.summary == orig.summary

    @given(
        score=st.integers(min_value=1, max_value=5),
        title=st.text(min_size=1, max_size=100),
        url=st.from_regex(r"https://[a-z0-9]+\.[a-z]{2,4}/[a-z]*", fullmatch=True),
        source=st.text(min_size=1, max_size=50),
        summary=st.text(max_size=200),
    )
    def test_scored_article_round_trip_via_research_brief(
        self,
        score: int,
        title: str,
        url: str,
        source: str,
        summary: str,
    ) -> None:
        """A ScoredArticle embedded in a ResearchBrief survives JSON round-trip."""
        brief = ResearchBrief(
            articles=[
                ScoredArticle(
                    title=title,
                    url=url,
                    source=source,
                    relevance_score=score,
                    summary=summary,
                )
            ],
            sources_crawled=[source],
            sources_failed=[],
            run_timestamp="2025-07-14T09:00:00+00:00",
        )
        restored = ResearchBrief.from_dict(json.loads(json.dumps(brief.to_dict())))
        a = restored.articles[0]
        assert isinstance(a.relevance_score, int)
        assert 1 <= a.relevance_score <= 5
        assert a.relevance_score == score
        assert a.title == title
        assert a.url == url
        assert a.source == source
        assert a.summary == summary
