"""Tests for user_interaction module.

Covers: format_article_list, parse_removal_input, present_scored_articles,
        format_output_choices, prompt_output_choice.
Requirements: REQ-008.1, REQ-008.2, REQ-008.3, REQ-008.4,
             REQ-009.1, REQ-009.2, REQ-009.3
"""

from __future__ import annotations

from datetime import date

from magic_content_engine.models import Article
from magic_content_engine.user_interaction import (
    OUTPUT_OPTIONS,
    UNATTENDED_DEFAULTS,
    format_article_list,
    format_output_choices,
    parse_removal_input,
    present_scored_articles,
    prompt_output_choice,
)


def _make_article(
    title: str = "Test Article",
    source: str = "kiro.dev/changelog/ide/",
    score: int = 4,
    rationale: str = "Relevant to Kiro IDE niche.",
) -> Article:
    return Article(
        url=f"https://example.com/{title.lower().replace(' ', '-')}",
        title=title,
        source=source,
        source_type="primary",
        discovered_date=date(2025, 7, 14),
        relevance_score=score,
        scoring_rationale=rationale,
        status="scored",
    )


# ---------------------------------------------------------------------------
# format_article_list
# ---------------------------------------------------------------------------


class TestFormatArticleList:
    def test_empty_list(self):
        assert format_article_list([]) == "No articles to display."

    def test_single_article_shows_all_fields(self):
        article = _make_article(title="Kiro Update", source="kiro.dev", score=5)
        result = format_article_list([article])
        assert "[1]" in result
        assert "Kiro Update" in result
        assert "kiro.dev" in result
        assert "Score: 5" in result
        assert "Relevant to Kiro IDE niche." in result

    def test_multiple_articles_numbered_sequentially(self):
        articles = [_make_article(title=f"Article {i}") for i in range(1, 4)]
        result = format_article_list(articles)
        assert "[1]" in result
        assert "[2]" in result
        assert "[3]" in result

    def test_missing_rationale_shows_fallback(self):
        article = _make_article()
        article.scoring_rationale = None
        result = format_article_list([article])
        assert "No summary available." in result


# ---------------------------------------------------------------------------
# parse_removal_input
# ---------------------------------------------------------------------------


class TestParseRemovalInput:
    def test_remove_with_commas(self):
        assert parse_removal_input("remove 1,3", 5) == [1, 3]

    def test_remove_with_spaces(self):
        assert parse_removal_input("remove 1 3", 5) == [1, 3]

    def test_r_shorthand(self):
        assert parse_removal_input("r 2,4", 5) == [2, 4]

    def test_numbers_only(self):
        assert parse_removal_input("1,3", 5) == [1, 3]

    def test_numbers_with_spaces(self):
        assert parse_removal_input("1 3", 5) == [1, 3]

    def test_out_of_range_ignored(self):
        assert parse_removal_input("remove 0,3,6", 5) == [3]

    def test_duplicates_deduplicated(self):
        assert parse_removal_input("remove 2,2,2", 5) == [2]

    def test_empty_input(self):
        assert parse_removal_input("", 5) == []

    def test_non_numeric_ignored(self):
        assert parse_removal_input("remove abc 2", 5) == [2]

    def test_mixed_separators(self):
        assert parse_removal_input("r 1, 3 5", 5) == [1, 3, 5]


# ---------------------------------------------------------------------------
# present_scored_articles
# ---------------------------------------------------------------------------


class TestPresentScoredArticles:
    def test_empty_list_returns_empty(self):
        confirmed, removed = present_scored_articles([], input_fn=lambda _: "y")
        assert confirmed == []
        assert removed == []

    def test_confirm_with_enter(self):
        articles = [_make_article(title="A1"), _make_article(title="A2")]
        confirmed, removed = present_scored_articles(articles, input_fn=lambda _: "")
        assert len(confirmed) == 2
        assert len(removed) == 0

    def test_confirm_with_y(self):
        articles = [_make_article(title="A1")]
        confirmed, removed = present_scored_articles(articles, input_fn=lambda _: "y")
        assert len(confirmed) == 1
        assert removed == []

    def test_confirm_with_yes(self):
        articles = [_make_article(title="A1")]
        confirmed, removed = present_scored_articles(
            articles, input_fn=lambda _: "yes"
        )
        assert len(confirmed) == 1

    def test_remove_single_article(self):
        articles = [_make_article(title="A1"), _make_article(title="A2")]
        inputs = iter(["remove 1", "y"])
        confirmed, removed = present_scored_articles(
            articles, input_fn=lambda _: next(inputs)
        )
        assert len(confirmed) == 1
        assert confirmed[0].title == "A2"
        assert len(removed) == 1
        assert removed[0].title == "A1"

    def test_remove_multiple_articles(self):
        articles = [
            _make_article(title="A1"),
            _make_article(title="A2"),
            _make_article(title="A3"),
        ]
        inputs = iter(["remove 1,3", "y"])
        confirmed, removed = present_scored_articles(
            articles, input_fn=lambda _: next(inputs)
        )
        assert len(confirmed) == 1
        assert confirmed[0].title == "A2"
        assert len(removed) == 2

    def test_remove_all_articles(self):
        articles = [_make_article(title="A1"), _make_article(title="A2")]
        confirmed, removed = present_scored_articles(
            articles, input_fn=lambda _: "remove 1,2"
        )
        assert confirmed == []
        assert len(removed) == 2

    def test_invalid_removal_reprompts(self):
        articles = [_make_article(title="A1")]
        inputs = iter(["remove 99", "y"])
        confirmed, removed = present_scored_articles(
            articles, input_fn=lambda _: next(inputs)
        )
        assert len(confirmed) == 1
        assert removed == []

    def test_original_articles_not_mutated(self):
        articles = [_make_article(title="A1"), _make_article(title="A2")]
        original_len = len(articles)
        present_scored_articles(articles, input_fn=lambda _: "remove 1,2")
        assert len(articles) == original_len


# ---------------------------------------------------------------------------
# Output choice constants
# ---------------------------------------------------------------------------


class TestOutputConstants:
    def test_output_options_has_five_entries(self):
        assert len(OUTPUT_OPTIONS) == 5

    def test_output_options_values(self):
        assert OUTPUT_OPTIONS == {
            1: "blog",
            2: "youtube",
            3: "cfp",
            4: "usergroup",
            5: "digest",
        }

    def test_unattended_defaults(self):
        assert UNATTENDED_DEFAULTS == ["blog", "youtube"]


# ---------------------------------------------------------------------------
# format_output_choices
# ---------------------------------------------------------------------------


class TestFormatOutputChoices:
    def test_contains_all_six_options(self):
        result = format_output_choices()
        assert "[1]" in result
        assert "[2]" in result
        assert "[3]" in result
        assert "[4]" in result
        assert "[5]" in result
        assert "[6]" in result

    def test_contains_option_labels(self):
        result = format_output_choices()
        assert "Blog" in result
        assert "YouTube" in result
        assert "CFP" in result
        assert "User group" in result
        assert "digest" in result.lower()
        assert "All" in result


# ---------------------------------------------------------------------------
# prompt_output_choice
# ---------------------------------------------------------------------------


class TestPromptOutputChoice:
    def test_unattended_returns_defaults(self):
        result = prompt_output_choice(unattended=True)
        assert result == ["blog", "youtube"]

    def test_unattended_does_not_call_input(self):
        def fail_input(_: str) -> str:
            raise AssertionError("input_fn should not be called in unattended mode")

        result = prompt_output_choice(input_fn=fail_input, unattended=True)
        assert result == ["blog", "youtube"]

    def test_select_single_option(self):
        result = prompt_output_choice(input_fn=lambda _: "1")
        assert result == ["blog"]

    def test_select_multiple_comma_separated(self):
        result = prompt_output_choice(input_fn=lambda _: "1,3")
        assert result == ["blog", "cfp"]

    def test_select_multiple_space_separated(self):
        result = prompt_output_choice(input_fn=lambda _: "1 3")
        assert result == ["blog", "cfp"]

    def test_select_all_with_option_6(self):
        result = prompt_output_choice(input_fn=lambda _: "6")
        assert result == ["blog", "youtube", "cfp", "usergroup", "digest"]

    def test_select_all_mixed_with_6(self):
        result = prompt_output_choice(input_fn=lambda _: "1,6")
        assert result == ["blog", "youtube", "cfp", "usergroup", "digest"]

    def test_invalid_then_valid_input(self):
        inputs = iter(["abc", "2"])
        result = prompt_output_choice(input_fn=lambda _: next(inputs))
        assert result == ["youtube"]

    def test_out_of_range_ignored(self):
        result = prompt_output_choice(input_fn=lambda _: "2,9")
        assert result == ["youtube"]

    def test_all_five_individually(self):
        result = prompt_output_choice(input_fn=lambda _: "1,2,3,4,5")
        assert result == ["blog", "youtube", "cfp", "usergroup", "digest"]

    def test_unattended_returns_new_list(self):
        """Ensure unattended mode returns a copy, not the constant."""
        result = prompt_output_choice(unattended=True)
        result.append("cfp")
        assert UNATTENDED_DEFAULTS == ["blog", "youtube"]
