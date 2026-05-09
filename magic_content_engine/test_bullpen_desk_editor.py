"""Tests for the Desk Editor Lambda.

Covers:
- ContentBrief JSON round-trip serialisation
- editorial_angle is a non-empty string
- tone_guidance references voice rules
- Property-based test: ContentBrief JSON round-trip

Requirements: REQ-5, REQ-6 (bullpen-architecture spec)
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import tempfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from magic_content_engine.bullpen.models import ContentBrief, ResearchBrief, ScoredArticle
from magic_content_engine.bullpen.desk_editor import (
    _build_user_prompt,
    _parse_response,
    run_desk_editor,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_scored_article(
    title: str = "AgentCore Runtime Update",
    url: str = "https://aws.amazon.com/blogs/machine-learning/agentcore",
    source: str = "aws.amazon.com",
    relevance_score: int = 4,
    summary: str = "AWS announces AgentCore runtime improvements.",
) -> ScoredArticle:
    return ScoredArticle(
        title=title,
        url=url,
        source=source,
        relevance_score=relevance_score,
        summary=summary,
    )


def _make_research_brief(articles: list[ScoredArticle] | None = None) -> ResearchBrief:
    if articles is None:
        articles = [
            _make_scored_article(),
            _make_scored_article(
                title="Kiro IDE Changelog",
                url="https://kiro.dev/changelog",
                source="kiro.dev",
                relevance_score=5,
                summary="Kiro IDE ships new steering file features.",
            ),
        ]
    return ResearchBrief(
        articles=articles,
        sources_crawled=["https://aws.amazon.com", "https://kiro.dev"],
        sources_failed=[],
        run_timestamp="2025-07-14T09:00:00+00:00",
    )


def _make_content_brief(
    selected_articles: list[ScoredArticle] | None = None,
    editorial_angle: str = "Kiro IDE and AgentCore are reshaping how NZ builders work.",
    tone_guidance: str = "Use short sentences. No em-dashes. No banned phrases like leverage.",
    output_types: list[str] | None = None,
    run_timestamp: str = "2025-07-14T09:05:00+00:00",
) -> ContentBrief:
    if selected_articles is None:
        selected_articles = [_make_scored_article()]
    if output_types is None:
        output_types = ["blog", "youtube"]
    return ContentBrief(
        selected_articles=selected_articles,
        editorial_angle=editorial_angle,
        tone_guidance=tone_guidance,
        output_types=output_types,
        run_timestamp=run_timestamp,
    )


def _make_voice_steering_file(tmp_dir: pathlib.Path) -> pathlib.Path:
    """Write a minimal 01-niche-and-voice.md into tmp_dir."""
    steering_dir = tmp_dir / ".kiro" / "steering"
    steering_dir.mkdir(parents=True, exist_ok=True)
    voice_file = steering_dir / "01-niche-and-voice.md"
    voice_file.write_text(
        "# Voice rules\n"
        "- No em-dashes\n"
        "- Short sentences preferred\n"
        "- Never use: leverage, empower, unlock, dive into, game-changer\n",
        encoding="utf-8",
    )
    return steering_dir


def _fake_bedrock_response(payload: dict) -> MagicMock:
    """Build a mock boto3 bedrock-runtime response for the given payload."""
    body_bytes = json.dumps(payload).encode()
    mock_body = MagicMock()
    mock_body.read.return_value = body_bytes
    mock_response = MagicMock()
    mock_response.__getitem__ = lambda self, key: (
        mock_body if key == "body" else None
    )
    return mock_response


def _llm_response_dict(
    selected_articles: list[dict] | None = None,
    editorial_angle: str = "Kiro IDE is changing how Aotearoa builders work.",
    tone_guidance: str = "Use short sentences. No em-dashes. No banned phrases.",
    output_types: list[str] | None = None,
) -> dict:
    """Build a valid LLM response dict."""
    if selected_articles is None:
        selected_articles = [
            {
                "title": "AgentCore Runtime Update",
                "url": "https://aws.amazon.com/blogs/machine-learning/agentcore",
                "source": "aws.amazon.com",
                "relevance_score": 4,
                "summary": "AWS announces AgentCore runtime improvements.",
            }
        ]
    if output_types is None:
        output_types = ["blog"]
    return {
        "content": [
            {
                "text": json.dumps(
                    {
                        "selected_articles": selected_articles,
                        "editorial_angle": editorial_angle,
                        "tone_guidance": tone_guidance,
                        "output_types": output_types,
                    }
                )
            }
        ]
    }


# ---------------------------------------------------------------------------
# ContentBrief JSON round-trip serialisation
# ---------------------------------------------------------------------------


class TestContentBriefJsonRoundTrip:
    """ContentBrief serialises to JSON and deserialises back to an equivalent object."""

    def test_round_trip_basic(self) -> None:
        brief = _make_content_brief()
        as_dict = dataclasses.asdict(brief)
        serialised = json.dumps(as_dict)
        deserialised = json.loads(serialised)

        assert deserialised["editorial_angle"] == brief.editorial_angle
        assert deserialised["tone_guidance"] == brief.tone_guidance
        assert deserialised["output_types"] == brief.output_types
        assert deserialised["run_timestamp"] == brief.run_timestamp

    def test_round_trip_selected_articles(self) -> None:
        article = _make_scored_article()
        brief = _make_content_brief(selected_articles=[article])
        as_dict = dataclasses.asdict(brief)
        serialised = json.dumps(as_dict)
        deserialised = json.loads(serialised)

        assert len(deserialised["selected_articles"]) == 1
        recovered = deserialised["selected_articles"][0]
        assert recovered["title"] == article.title
        assert recovered["url"] == article.url
        assert recovered["source"] == article.source
        assert recovered["relevance_score"] == article.relevance_score
        assert recovered["summary"] == article.summary

    def test_round_trip_empty_articles(self) -> None:
        brief = _make_content_brief(selected_articles=[])
        as_dict = dataclasses.asdict(brief)
        serialised = json.dumps(as_dict)
        deserialised = json.loads(serialised)
        assert deserialised["selected_articles"] == []

    def test_round_trip_multiple_output_types(self) -> None:
        brief = _make_content_brief(output_types=["blog", "youtube", "cfp", "usergroup"])
        as_dict = dataclasses.asdict(brief)
        serialised = json.dumps(as_dict)
        deserialised = json.loads(serialised)
        assert deserialised["output_types"] == ["blog", "youtube", "cfp", "usergroup"]

    def test_round_trip_preserves_all_fields(self) -> None:
        brief = _make_content_brief()
        as_dict = dataclasses.asdict(brief)
        # All expected keys present
        assert set(as_dict.keys()) == {
            "selected_articles",
            "editorial_angle",
            "tone_guidance",
            "output_types",
            "run_timestamp",
        }


# ---------------------------------------------------------------------------
# editorial_angle is a non-empty string
# ---------------------------------------------------------------------------


class TestEditorialAngle:
    def test_editorial_angle_non_empty_from_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steering_dir = _make_voice_steering_file(pathlib.Path(tmp))
            research_brief = _make_research_brief()

            mock_client = MagicMock()
            mock_client.invoke_model.return_value = _fake_bedrock_response(
                _llm_response_dict(
                    editorial_angle="Kiro IDE is reshaping how Aotearoa builders work."
                )
            )

            brief = run_desk_editor(
                research_brief,
                topic="Kiro IDE",
                output_types=["blog"],
                steering_base_path=str(steering_dir),
                bedrock_client=mock_client,
            )

        assert isinstance(brief.editorial_angle, str)
        assert len(brief.editorial_angle.strip()) > 0

    def test_editorial_angle_raises_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steering_dir = _make_voice_steering_file(pathlib.Path(tmp))
            research_brief = _make_research_brief()

            mock_client = MagicMock()
            mock_client.invoke_model.return_value = _fake_bedrock_response(
                _llm_response_dict(editorial_angle="")
            )

            with pytest.raises(ValueError, match="editorial_angle"):
                run_desk_editor(
                    research_brief,
                    topic="Kiro IDE",
                    output_types=["blog"],
                    steering_base_path=str(steering_dir),
                    bedrock_client=mock_client,
                )

    def test_editorial_angle_is_string_type(self) -> None:
        brief = _make_content_brief(editorial_angle="Some angle about Aotearoa builders.")
        assert isinstance(brief.editorial_angle, str)


# ---------------------------------------------------------------------------
# tone_guidance references voice rules
# ---------------------------------------------------------------------------


class TestToneGuidance:
    """tone_guidance must reference at least one voice rule from the steering file."""

    _VOICE_RULE_KEYWORDS = [
        "em-dash",
        "em dash",
        "short sentence",
        "banned phrase",
        "leverage",
        "empower",
        "unlock",
        "dive into",
        "game-changer",
        "first-person",
        "conversational",
        "voice rule",
        "no em",
    ]

    def _has_voice_reference(self, tone_guidance: str) -> bool:
        lower = tone_guidance.lower()
        return any(kw in lower for kw in self._VOICE_RULE_KEYWORDS)

    def test_tone_guidance_references_voice_rules_from_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steering_dir = _make_voice_steering_file(pathlib.Path(tmp))
            research_brief = _make_research_brief()

            mock_client = MagicMock()
            mock_client.invoke_model.return_value = _fake_bedrock_response(
                _llm_response_dict(
                    tone_guidance=(
                        "Use short sentences. No em-dashes. "
                        "Avoid banned phrases like leverage or game-changer."
                    )
                )
            )

            brief = run_desk_editor(
                research_brief,
                topic="Kiro IDE",
                output_types=["blog"],
                steering_base_path=str(steering_dir),
                bedrock_client=mock_client,
            )

        assert self._has_voice_reference(brief.tone_guidance), (
            f"tone_guidance does not reference voice rules: {brief.tone_guidance!r}"
        )

    def test_tone_guidance_non_empty(self) -> None:
        brief = _make_content_brief(
            tone_guidance="Short sentences. No em-dashes. No banned phrases."
        )
        assert len(brief.tone_guidance.strip()) > 0

    def test_tone_guidance_raises_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steering_dir = _make_voice_steering_file(pathlib.Path(tmp))
            research_brief = _make_research_brief()

            mock_client = MagicMock()
            mock_client.invoke_model.return_value = _fake_bedrock_response(
                _llm_response_dict(tone_guidance="")
            )

            with pytest.raises(ValueError, match="tone_guidance"):
                run_desk_editor(
                    research_brief,
                    topic="Kiro IDE",
                    output_types=["blog"],
                    steering_base_path=str(steering_dir),
                    bedrock_client=mock_client,
                )


# ---------------------------------------------------------------------------
# run_desk_editor — integration-style unit tests
# ---------------------------------------------------------------------------


class TestRunDeskEditor:
    def test_returns_content_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steering_dir = _make_voice_steering_file(pathlib.Path(tmp))
            research_brief = _make_research_brief()

            mock_client = MagicMock()
            mock_client.invoke_model.return_value = _fake_bedrock_response(
                _llm_response_dict()
            )

            result = run_desk_editor(
                research_brief,
                topic="AgentCore",
                output_types=["blog"],
                steering_base_path=str(steering_dir),
                bedrock_client=mock_client,
            )

        assert isinstance(result, ContentBrief)

    def test_reads_steering_file_at_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steering_dir = _make_voice_steering_file(pathlib.Path(tmp))
            research_brief = _make_research_brief()

            mock_client = MagicMock()
            mock_client.invoke_model.return_value = _fake_bedrock_response(
                _llm_response_dict()
            )

            run_desk_editor(
                research_brief,
                topic="AgentCore",
                output_types=["blog"],
                steering_base_path=str(steering_dir),
                bedrock_client=mock_client,
            )

        # Verify the prompt included voice rules content
        call_args = mock_client.invoke_model.call_args
        body = json.loads(call_args.kwargs["body"])
        user_content = body["messages"][0]["content"]
        assert "Voice" in user_content or "voice" in user_content

    def test_raises_when_steering_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            research_brief = _make_research_brief()
            mock_client = MagicMock()

            with pytest.raises(FileNotFoundError, match="01-niche-and-voice.md"):
                run_desk_editor(
                    research_brief,
                    topic="AgentCore",
                    output_types=["blog"],
                    steering_base_path=str(pathlib.Path(tmp) / "nonexistent"),
                    bedrock_client=mock_client,
                )

    def test_uses_sonnet_model(self) -> None:
        from magic_content_engine.config import SONNET_MODEL_ID

        with tempfile.TemporaryDirectory() as tmp:
            steering_dir = _make_voice_steering_file(pathlib.Path(tmp))
            research_brief = _make_research_brief()

            mock_client = MagicMock()
            mock_client.invoke_model.return_value = _fake_bedrock_response(
                _llm_response_dict()
            )

            run_desk_editor(
                research_brief,
                topic="AgentCore",
                output_types=["blog"],
                steering_base_path=str(steering_dir),
                bedrock_client=mock_client,
            )

        call_args = mock_client.invoke_model.call_args
        assert call_args.kwargs["modelId"] == SONNET_MODEL_ID

    def test_run_timestamp_is_iso8601(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steering_dir = _make_voice_steering_file(pathlib.Path(tmp))
            research_brief = _make_research_brief()

            mock_client = MagicMock()
            mock_client.invoke_model.return_value = _fake_bedrock_response(
                _llm_response_dict()
            )

            result = run_desk_editor(
                research_brief,
                topic="AgentCore",
                output_types=["blog"],
                steering_base_path=str(steering_dir),
                bedrock_client=mock_client,
            )

        # Should parse without error
        datetime.fromisoformat(result.run_timestamp)

    def test_default_output_types_is_blog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steering_dir = _make_voice_steering_file(pathlib.Path(tmp))
            research_brief = _make_research_brief()

            mock_client = MagicMock()
            mock_client.invoke_model.return_value = _fake_bedrock_response(
                _llm_response_dict(output_types=["blog"])
            )

            result = run_desk_editor(
                research_brief,
                topic="AgentCore",
                # output_types not provided — should default to ["blog"]
                steering_base_path=str(steering_dir),
                bedrock_client=mock_client,
            )

        assert "blog" in result.output_types

    def test_selected_articles_are_scored_articles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steering_dir = _make_voice_steering_file(pathlib.Path(tmp))
            research_brief = _make_research_brief()

            mock_client = MagicMock()
            mock_client.invoke_model.return_value = _fake_bedrock_response(
                _llm_response_dict()
            )

            result = run_desk_editor(
                research_brief,
                topic="AgentCore",
                output_types=["blog"],
                steering_base_path=str(steering_dir),
                bedrock_client=mock_client,
            )

        for article in result.selected_articles:
            assert isinstance(article, ScoredArticle)
            assert 1 <= article.relevance_score <= 5


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------


class TestParseResponse:
    def test_valid_json_parsed(self) -> None:
        payload = {"editorial_angle": "Test angle", "tone_guidance": "Short sentences."}
        result = _parse_response(json.dumps(payload))
        assert result["editorial_angle"] == "Test angle"

    def test_strips_markdown_fences(self) -> None:
        payload = {"editorial_angle": "Test", "tone_guidance": "Short."}
        fenced = f"```json\n{json.dumps(payload)}\n```"
        result = _parse_response(fenced)
        assert result["editorial_angle"] == "Test"

    def test_strips_plain_fences(self) -> None:
        payload = {"editorial_angle": "Test", "tone_guidance": "Short."}
        fenced = f"```\n{json.dumps(payload)}\n```"
        result = _parse_response(fenced)
        assert result["editorial_angle"] == "Test"

    def test_invalid_json_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            _parse_response("not json at all")


# ---------------------------------------------------------------------------
# _build_user_prompt
# ---------------------------------------------------------------------------


class TestBuildUserPrompt:
    def test_includes_topic(self) -> None:
        brief = _make_research_brief()
        prompt = _build_user_prompt(brief, "Kiro IDE", "# Voice rules\n- No em-dashes", ["blog"])
        assert "Kiro IDE" in prompt

    def test_includes_voice_rules(self) -> None:
        brief = _make_research_brief()
        voice = "# Voice rules\n- No em-dashes\n- Short sentences"
        prompt = _build_user_prompt(brief, "AgentCore", voice, ["blog"])
        assert "No em-dashes" in prompt

    def test_includes_article_titles(self) -> None:
        brief = _make_research_brief()
        prompt = _build_user_prompt(brief, "AgentCore", "# Voice", ["blog"])
        assert "AgentCore Runtime Update" in prompt

    def test_includes_output_types(self) -> None:
        brief = _make_research_brief()
        prompt = _build_user_prompt(brief, "AgentCore", "# Voice", ["blog", "youtube"])
        assert "blog" in prompt
        assert "youtube" in prompt


# ---------------------------------------------------------------------------
# Property-based test: ContentBrief JSON round-trip
# ---------------------------------------------------------------------------

# Feature: desk-editor-lambda, Property 1: ContentBrief JSON round-trip
# Validates: REQ-6.3 (bullpen-architecture spec)


@st.composite
def scored_article_strategy(draw: Any) -> ScoredArticle:
    return ScoredArticle(
        title=draw(st.text(min_size=1, max_size=200)),
        url=draw(
            st.from_regex(r"https://[a-z0-9\-]+\.[a-z]{2,6}/[a-z0-9\-/]*", fullmatch=True).filter(
                lambda u: len(u) <= 200
            )
        ),
        source=draw(st.text(min_size=1, max_size=100)),
        relevance_score=draw(st.integers(min_value=1, max_value=5)),
        summary=draw(st.text(min_size=1, max_size=500)),
    )


@st.composite
def content_brief_strategy(draw: Any) -> ContentBrief:
    articles = draw(st.lists(scored_article_strategy(), min_size=0, max_size=5))
    output_types = draw(
        st.lists(
            st.sampled_from(["blog", "youtube", "cfp", "usergroup", "digest"]),
            min_size=1,
            max_size=5,
            unique=True,
        )
    )
    return ContentBrief(
        selected_articles=articles,
        editorial_angle=draw(st.text(min_size=1, max_size=500)),
        tone_guidance=draw(st.text(min_size=1, max_size=500)),
        output_types=output_types,
        run_timestamp=draw(
            st.datetimes(timezones=st.just(timezone.utc)).map(lambda dt: dt.isoformat())
        ),
    )


@given(brief=content_brief_strategy())
@settings(max_examples=100)
def test_content_brief_json_roundtrip(brief: ContentBrief) -> None:
    """FOR ALL valid ContentBriefs, JSON round-trip produces an equivalent object.

    Validates REQ-6.3: serialising the brief to JSON and deserialising it back
    SHALL produce an object equivalent to the original.
    """
    as_dict = dataclasses.asdict(brief)
    serialised = json.dumps(as_dict)
    deserialised = json.loads(serialised)

    # Reconstruct from deserialised dict
    recovered_articles = [
        ScoredArticle(
            title=a["title"],
            url=a["url"],
            source=a["source"],
            relevance_score=a["relevance_score"],
            summary=a["summary"],
        )
        for a in deserialised["selected_articles"]
    ]
    recovered = ContentBrief(
        selected_articles=recovered_articles,
        editorial_angle=deserialised["editorial_angle"],
        tone_guidance=deserialised["tone_guidance"],
        output_types=deserialised["output_types"],
        run_timestamp=deserialised["run_timestamp"],
    )

    # Field-by-field equivalence
    assert recovered.editorial_angle == brief.editorial_angle
    assert recovered.tone_guidance == brief.tone_guidance
    assert recovered.output_types == brief.output_types
    assert recovered.run_timestamp == brief.run_timestamp
    assert len(recovered.selected_articles) == len(brief.selected_articles)
    for orig, rec in zip(brief.selected_articles, recovered.selected_articles):
        assert rec.title == orig.title
        assert rec.url == orig.url
        assert rec.source == orig.source
        assert rec.relevance_score == orig.relevance_score
        assert rec.summary == orig.summary
