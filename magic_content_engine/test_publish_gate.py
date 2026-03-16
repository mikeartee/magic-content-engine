"""Tests for the Publish Gate review module.

Covers: format_output_preview, prompt_publish_gate (all 4 decisions),
run_publish_gate (interactive + unattended), and the FileOps protocol.

Requirements: REQ-030.1–REQ-030.8
"""

from __future__ import annotations

from datetime import date

import pytest

from magic_content_engine.publish_gate import (
    DefaultFileOps,
    PublishGateDecision,
    PublishGateResult,
    format_output_preview,
    prompt_publish_gate,
    run_publish_gate,
    _parse_date,
)
from magic_content_engine.models import HeldItem, ReviewItem


# ---------------------------------------------------------------------------
# Fake FileOps for testing (no real filesystem moves)
# ---------------------------------------------------------------------------


class FakeFileOps:
    """Records move calls instead of touching the filesystem."""

    def __init__(self) -> None:
        self.moves: list[tuple[str, str, str]] = []

    def move_file(self, src: str, dest_dir: str, filename: str) -> str:
        self.moves.append((src, dest_dir, filename))
        return f"{dest_dir}/{filename}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_CONTENT = "First line of content\nSecond line here\nThird line now\nFourth line extra"
RUN_DATE = date(2025, 7, 14)
SLUG = "agentcore-launch"


# ---------------------------------------------------------------------------
# format_output_preview
# ---------------------------------------------------------------------------


class TestFormatOutputPreview:
    def test_shows_filename_and_word_count(self):
        preview = format_output_preview("post.md", SAMPLE_CONTENT)
        assert "post.md" in preview
        assert "Words: 13" in preview

    def test_shows_first_three_lines(self):
        preview = format_output_preview("post.md", SAMPLE_CONTENT)
        assert "First line of content" in preview
        assert "Second line here" in preview
        assert "Third line now" in preview
        assert "Fourth line extra" not in preview

    def test_empty_content(self):
        preview = format_output_preview("empty.md", "")
        assert "empty.md" in preview
        assert "Words: 0" in preview

    def test_fewer_than_three_lines(self):
        preview = format_output_preview("short.md", "Only one line")
        assert "Only one line" in preview


# ---------------------------------------------------------------------------
# _parse_date
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_valid_date(self):
        assert _parse_date("2025-07-14") == date(2025, 7, 14)

    def test_invalid_date(self):
        assert _parse_date("not-a-date") is None

    def test_empty_string(self):
        assert _parse_date("") is None

    def test_whitespace_stripped(self):
        assert _parse_date("  2025-01-01  ") == date(2025, 1, 1)


# ---------------------------------------------------------------------------
# prompt_publish_gate — Approve
# ---------------------------------------------------------------------------


class TestPromptApprove:
    def test_approve_returns_correct_result(self):
        result = prompt_publish_gate(
            filename="post.md",
            content=SAMPLE_CONTENT,
            slug=SLUG,
            run_date=RUN_DATE,
            input_fn=lambda _: "1",
        )
        assert result.decision == PublishGateDecision.APPROVE
        assert result.filename == "post.md"
        assert result.held_item is None
        assert result.review_item is None
        assert result.release_date is None


# ---------------------------------------------------------------------------
# prompt_publish_gate — Skip
# ---------------------------------------------------------------------------


class TestPromptSkip:
    def test_skip_returns_correct_result(self):
        result = prompt_publish_gate(
            filename="script.md",
            content=SAMPLE_CONTENT,
            slug=SLUG,
            run_date=RUN_DATE,
            input_fn=lambda _: "2",
        )
        assert result.decision == PublishGateDecision.SKIP
        assert result.filename == "script.md"


# ---------------------------------------------------------------------------
# prompt_publish_gate — Hold
# ---------------------------------------------------------------------------


class TestPromptHold:
    def test_hold_creates_held_item(self):
        responses = iter(["3", "2025-08-01"])
        fake_ops = FakeFileOps()

        result = prompt_publish_gate(
            filename="post.md",
            content=SAMPLE_CONTENT,
            slug=SLUG,
            run_date=RUN_DATE,
            bundle_dir="/tmp/bundle",
            s3_key_prefix="output/2025-07-14-agentcore-launch/",
            article_titles=["Article A", "Article B"],
            file_ops=fake_ops,
            input_fn=lambda _: next(responses),
        )

        assert result.decision == PublishGateDecision.HOLD
        assert result.release_date == date(2025, 8, 1)
        assert result.held_item is not None
        assert result.held_item.filename == "post.md"
        assert result.held_item.release_date == date(2025, 8, 1)
        assert result.held_item.article_titles == ["Article A", "Article B"]
        assert result.held_item.run_date == RUN_DATE
        assert result.held_item.s3_destination_path == "output/2025-07-14-agentcore-launch/post.md"
        assert len(fake_ops.moves) == 1

    def test_hold_retries_on_bad_date(self):
        responses = iter(["3", "bad-date", "2025-09-15"])
        fake_ops = FakeFileOps()

        result = prompt_publish_gate(
            filename="post.md",
            content=SAMPLE_CONTENT,
            slug=SLUG,
            run_date=RUN_DATE,
            file_ops=fake_ops,
            input_fn=lambda _: next(responses),
        )

        assert result.decision == PublishGateDecision.HOLD
        assert result.release_date == date(2025, 9, 15)


# ---------------------------------------------------------------------------
# prompt_publish_gate — Review
# ---------------------------------------------------------------------------


class TestPromptReview:
    def test_review_creates_review_item(self):
        fake_ops = FakeFileOps()

        result = prompt_publish_gate(
            filename="cfp-proposal.md",
            content=SAMPLE_CONTENT,
            slug=SLUG,
            run_date=RUN_DATE,
            bundle_dir="/tmp/bundle",
            file_ops=fake_ops,
            input_fn=lambda _: "4",
        )

        assert result.decision == PublishGateDecision.REVIEW
        assert result.review_item is not None
        assert result.review_item.filename == "cfp-proposal.md"
        assert result.review_item.run_date == RUN_DATE
        assert result.review_item.reason == "held for manual review"
        assert len(fake_ops.moves) == 1


# ---------------------------------------------------------------------------
# prompt_publish_gate — invalid input retries
# ---------------------------------------------------------------------------


class TestPromptInvalidInput:
    def test_retries_on_invalid_then_accepts(self):
        responses = iter(["x", "7", "1"])

        result = prompt_publish_gate(
            filename="post.md",
            content=SAMPLE_CONTENT,
            slug=SLUG,
            run_date=RUN_DATE,
            input_fn=lambda _: next(responses),
        )

        assert result.decision == PublishGateDecision.APPROVE


# ---------------------------------------------------------------------------
# run_publish_gate — interactive mode
# ---------------------------------------------------------------------------


class TestRunPublishGateInteractive:
    def test_processes_all_outputs(self):
        outputs = {
            "post.md": "Blog content here",
            "script.md": "Script content here",
        }
        # Approve first, skip second
        responses = iter(["1", "2"])

        results = run_publish_gate(
            outputs=outputs,
            slug=SLUG,
            run_date=RUN_DATE,
            unattended=False,
            input_fn=lambda _: next(responses),
        )

        assert len(results) == 2
        assert results[0].decision == PublishGateDecision.APPROVE
        assert results[0].filename == "post.md"
        assert results[1].decision == PublishGateDecision.SKIP
        assert results[1].filename == "script.md"


# ---------------------------------------------------------------------------
# run_publish_gate — unattended mode (REQ-030.8)
# ---------------------------------------------------------------------------


class TestRunPublishGateUnattended:
    def test_unattended_skips_all_no_auto_approve(self):
        outputs = {
            "post.md": "Blog content",
            "script.md": "Script content",
            "digest-email.txt": "Digest content",
        }

        results = run_publish_gate(
            outputs=outputs,
            slug=SLUG,
            run_date=RUN_DATE,
            unattended=True,
            input_fn=lambda _: pytest.fail("Should not prompt in unattended mode"),
        )

        assert len(results) == 3
        for r in results:
            assert r.decision == PublishGateDecision.SKIP

    def test_unattended_empty_outputs(self):
        results = run_publish_gate(
            outputs={},
            slug=SLUG,
            run_date=RUN_DATE,
            unattended=True,
        )
        assert results == []
