"""Unit tests for Weekly Brief generator.

Tests cover:
- generate_weekly_brief() with engagements and without (clean state)
- format_weekly_brief() terminal output formatting
- Top performing content section presence/omission
- Topic coverage map display (covered and gaps)
- Recommended focus derivation
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from magic_content_engine.models import (
    PostEngagement,
    TopicCoverageEntry,
    TopicCoverageMap,
    WeeklyBrief,
)
from magic_content_engine.topic_coverage import NICHE_TOPICS, create_empty_coverage_map
from magic_content_engine.weekly_brief import (
    CLEAN_STATE_MESSAGE,
    format_weekly_brief,
    generate_weekly_brief,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RUN_DATE = date(2025, 7, 14)


def _make_engagement(
    title: str = "Test Post",
    pub_date: Optional[date] = None,
    views: int = 100,
    reactions: int = 10,
) -> PostEngagement:
    return PostEngagement(
        post_title=title,
        publication_date=pub_date or RUN_DATE,
        url=f"https://dev.to/mike/{title.lower().replace(' ', '-')}",
        views=views,
        reactions=reactions,
        comments=2,
        reading_time_minutes=4,
        last_fetched=RUN_DATE,
    )


def _make_entry(
    topic: str,
    covered: bool = False,
    last_date: Optional[date] = None,
    adjacent: Optional[list[str]] = None,
) -> TopicCoverageEntry:
    return TopicCoverageEntry(
        topic=topic,
        covered=covered,
        article_titles=[],
        last_covered_date=last_date,
        adjacent_topics=adjacent or [],
    )


# ---------------------------------------------------------------------------
# Tests — generate_weekly_brief
# ---------------------------------------------------------------------------


class TestGenerateWeeklyBrief:
    def test_clean_state_when_no_engagements(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        brief = generate_weekly_brief(coverage, [], RUN_DATE)
        assert brief.clean_state is True
        assert brief.top_post is None
        assert brief.run_date == RUN_DATE

    def test_not_clean_state_when_engagements_exist(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        eng = _make_engagement("My Post", pub_date=RUN_DATE - timedelta(days=2))
        brief = generate_weekly_brief(coverage, [eng], RUN_DATE)
        assert brief.clean_state is False

    def test_top_post_identified_from_engagements(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        low = _make_engagement("Low", pub_date=RUN_DATE - timedelta(days=1), views=10, reactions=2)
        high = _make_engagement("High", pub_date=RUN_DATE - timedelta(days=1), views=500, reactions=80)
        brief = generate_weekly_brief(coverage, [low, high], RUN_DATE)
        assert brief.top_post is not None
        assert brief.top_post.post_title == "High"

    def test_top_post_none_when_all_outside_window(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        old = _make_engagement("Old", pub_date=RUN_DATE - timedelta(days=30))
        brief = generate_weekly_brief(coverage, [old], RUN_DATE)
        assert brief.clean_state is False
        assert brief.top_post is None

    def test_recommended_focus_derived_from_gaps(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        brief = generate_weekly_brief(coverage, [], RUN_DATE)
        assert brief.recommended_focus is not None
        assert brief.recommended_focus in NICHE_TOPICS

    def test_recommended_focus_fallback_when_all_covered(self) -> None:
        today = date.today()
        coverage = TopicCoverageMap(
            entries=[_make_entry(t, covered=True, last_date=today) for t in NICHE_TOPICS],
            last_updated=today,
        )
        brief = generate_weekly_brief(coverage, [], today)
        assert brief.recommended_focus == "Kiro IDE"

    def test_user_override_defaults_to_none(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        brief = generate_weekly_brief(coverage, [], RUN_DATE)
        assert brief.user_override is None


# ---------------------------------------------------------------------------
# Tests — format_weekly_brief
# ---------------------------------------------------------------------------


class TestFormatWeeklyBrief:
    def test_header_contains_date(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        brief = generate_weekly_brief(coverage, [], RUN_DATE)
        output = format_weekly_brief(brief)
        assert "Weekly brief — 2025-07-14" in output

    def test_clean_state_shows_message(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        brief = generate_weekly_brief(coverage, [], RUN_DATE)
        output = format_weekly_brief(brief)
        assert CLEAN_STATE_MESSAGE in output
        assert "Top performing content (past 7 days):" not in output

    def test_top_post_displayed_when_present(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        eng = _make_engagement("Strands SDK Guide", pub_date=RUN_DATE - timedelta(days=1), views=250, reactions=42)
        brief = generate_weekly_brief(coverage, [eng], RUN_DATE)
        output = format_weekly_brief(brief)
        assert "Top performing content (past 7 days):" in output
        assert "Strands SDK Guide" in output
        assert "250 views" in output
        assert "42 reactions" in output

    def test_no_recent_posts_message(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        old = _make_engagement("Old", pub_date=RUN_DATE - timedelta(days=30))
        brief = generate_weekly_brief(coverage, [old], RUN_DATE)
        output = format_weekly_brief(brief)
        assert "No posts in the past 7 days." in output

    def test_coverage_map_shows_covered_topics(self) -> None:
        coverage = TopicCoverageMap(
            entries=[
                _make_entry("Kiro IDE", covered=True, last_date=date(2025, 7, 10)),
                _make_entry("Bedrock", covered=False),
            ],
            last_updated=RUN_DATE,
        )
        brief = generate_weekly_brief(coverage, [], RUN_DATE)
        output = format_weekly_brief(brief)
        assert "Covered: Kiro IDE (2025-07-10)" in output
        assert "Not yet covered: Bedrock" in output

    def test_coverage_map_all_uncovered(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        brief = generate_weekly_brief(coverage, [], RUN_DATE)
        output = format_weekly_brief(brief)
        assert "Covered: (none)" in output
        assert "Not yet covered:" in output

    def test_recommended_focus_displayed(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        brief = generate_weekly_brief(coverage, [], RUN_DATE)
        output = format_weekly_brief(brief)
        assert "Recommended focus this week:" in output
        assert brief.recommended_focus in output

    def test_prompt_displayed(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        brief = generate_weekly_brief(coverage, [], RUN_DATE)
        output = format_weekly_brief(brief)
        assert "Press Enter to accept, or type a different topic:" in output


# ---------------------------------------------------------------------------
# Tests — prompt_user_focus (REQ-035.4)
# ---------------------------------------------------------------------------

from magic_content_engine.weekly_brief import (
    brief_to_log_dict,
    get_effective_focus,
    prompt_user_focus,
)


def _make_brief(override: Optional[str] = None) -> WeeklyBrief:
    """Create a minimal WeeklyBrief for testing."""
    coverage = create_empty_coverage_map(RUN_DATE)
    brief = generate_weekly_brief(coverage, [], RUN_DATE)
    brief.user_override = override
    return brief


class TestPromptUserFocus:
    def test_enter_accepts_recommended_focus(self, monkeypatch) -> None:
        """Pressing Enter (empty input) leaves user_override as None."""
        brief = _make_brief()
        monkeypatch.setattr("builtins.input", lambda: "")
        result = prompt_user_focus(brief)
        assert result.user_override is None

    def test_typed_topic_sets_override(self, monkeypatch) -> None:
        """Typing a topic stores it in user_override."""
        brief = _make_brief()
        monkeypatch.setattr("builtins.input", lambda: "Strands SDK")
        result = prompt_user_focus(brief)
        assert result.user_override == "Strands SDK"

    def test_whitespace_only_treated_as_accept(self, monkeypatch) -> None:
        """Whitespace-only input is treated as accepting the recommendation."""
        brief = _make_brief()
        monkeypatch.setattr("builtins.input", lambda: "   ")
        result = prompt_user_focus(brief)
        assert result.user_override is None

    def test_prints_formatted_brief(self, monkeypatch, capsys) -> None:
        """prompt_user_focus prints the formatted brief to stdout."""
        brief = _make_brief()
        monkeypatch.setattr("builtins.input", lambda: "")
        prompt_user_focus(brief)
        captured = capsys.readouterr()
        assert "Weekly brief" in captured.out
        assert "Press Enter to accept" in captured.out


# ---------------------------------------------------------------------------
# Tests — get_effective_focus (REQ-035.5)
# ---------------------------------------------------------------------------


class TestGetEffectiveFocus:
    def test_returns_recommended_when_no_override(self) -> None:
        brief = _make_brief()
        assert get_effective_focus(brief) == brief.recommended_focus

    def test_returns_override_when_set(self) -> None:
        brief = _make_brief(override="MCP Protocol")
        assert get_effective_focus(brief) == "MCP Protocol"


# ---------------------------------------------------------------------------
# Tests — brief_to_log_dict (REQ-035.6)
# ---------------------------------------------------------------------------


class TestBriefToLogDict:
    def test_contains_required_keys(self) -> None:
        brief = _make_brief()
        log = brief_to_log_dict(brief)
        assert set(log.keys()) == {
            "run_date",
            "recommended_focus",
            "user_override",
            "clean_state",
            "top_post_title",
        }

    def test_run_date_serialised_as_iso(self) -> None:
        brief = _make_brief()
        log = brief_to_log_dict(brief)
        assert log["run_date"] == "2025-07-14"

    def test_override_none_when_not_set(self) -> None:
        brief = _make_brief()
        log = brief_to_log_dict(brief)
        assert log["user_override"] is None

    def test_override_recorded_when_set(self) -> None:
        brief = _make_brief(override="AgentCore Gateway")
        log = brief_to_log_dict(brief)
        assert log["user_override"] == "AgentCore Gateway"

    def test_top_post_title_none_in_clean_state(self) -> None:
        brief = _make_brief()
        log = brief_to_log_dict(brief)
        assert log["top_post_title"] is None

    def test_top_post_title_present_when_engagement_exists(self) -> None:
        coverage = create_empty_coverage_map(RUN_DATE)
        eng = _make_engagement("My Great Post", pub_date=RUN_DATE - timedelta(days=1))
        brief = generate_weekly_brief(coverage, [eng], RUN_DATE)
        log = brief_to_log_dict(brief)
        assert log["top_post_title"] == "My Great Post"

    def test_clean_state_flag_recorded(self) -> None:
        brief = _make_brief()
        log = brief_to_log_dict(brief)
        assert log["clean_state"] is True
