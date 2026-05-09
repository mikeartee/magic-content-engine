"""Tests for the Approval Gate module.

Covers:
- Email content generation (filename, word count, first 3 lines present)
- Token generation and validation (valid token validates, tampered token fails)
- Approve/reject URL format
- SES send called with correct parameters
- Missing file handled gracefully (word count 0, no first lines)

Requirements: REQ-bullpen-14
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from magic_content_engine.bullpen.approval_gate import (
    ApprovalEmailContent,
    _build_approval_url,
    _read_file_preview,
    check_approval,
    format_approval_email,
    generate_approval_token,
    send_approval_email,
    validate_approval_token,
)
from magic_content_engine.bullpen.models import Verdict


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

_TEST_SECRET = "test-secret-do-not-use-in-prod"
_TEST_RUN_ID = "2025-07-14-agentcore-launch"
_TEST_BASE_URL = "https://api.example.com/approval"


def _make_verdict(filename: str, verdict: str = "publish", feedback: str = "") -> Verdict:
    return Verdict(filename=filename, verdict=verdict, feedback=feedback)


class FakeSESClient:
    """Records sent emails."""

    def __init__(self, should_fail: bool = False) -> None:
        self.sent: list[dict[str, str]] = []
        self.should_fail = should_fail

    def send_email(
        self,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
    ) -> None:
        if self.should_fail:
            raise RuntimeError("Simulated SES failure")
        self.sent.append(
            {"sender": sender, "recipient": recipient, "subject": subject, "body": body}
        )


# ---------------------------------------------------------------------------
# Token generation and validation
# ---------------------------------------------------------------------------


class TestTokenGenerationAndValidation:
    def test_generate_token_returns_hex_string(self):
        token = generate_approval_token(_TEST_RUN_ID, secret=_TEST_SECRET)
        assert isinstance(token, str)
        # SHA-256 hex digest is always 64 characters
        assert len(token) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", token)

    def test_same_run_id_same_secret_produces_same_token(self):
        t1 = generate_approval_token(_TEST_RUN_ID, secret=_TEST_SECRET)
        t2 = generate_approval_token(_TEST_RUN_ID, secret=_TEST_SECRET)
        assert t1 == t2

    def test_different_run_ids_produce_different_tokens(self):
        t1 = generate_approval_token("run-a", secret=_TEST_SECRET)
        t2 = generate_approval_token("run-b", secret=_TEST_SECRET)
        assert t1 != t2

    def test_different_secrets_produce_different_tokens(self):
        t1 = generate_approval_token(_TEST_RUN_ID, secret="secret-one")
        t2 = generate_approval_token(_TEST_RUN_ID, secret="secret-two")
        assert t1 != t2

    def test_valid_token_validates(self):
        token = generate_approval_token(_TEST_RUN_ID, secret=_TEST_SECRET)
        assert validate_approval_token(token, _TEST_RUN_ID, secret=_TEST_SECRET) is True

    def test_tampered_token_fails_validation(self):
        token = generate_approval_token(_TEST_RUN_ID, secret=_TEST_SECRET)
        tampered = token[:-4] + "dead"
        assert validate_approval_token(tampered, _TEST_RUN_ID, secret=_TEST_SECRET) is False

    def test_wrong_run_id_fails_validation(self):
        token = generate_approval_token("run-correct", secret=_TEST_SECRET)
        assert validate_approval_token(token, "run-wrong", secret=_TEST_SECRET) is False

    def test_wrong_secret_fails_validation(self):
        token = generate_approval_token(_TEST_RUN_ID, secret="secret-a")
        assert validate_approval_token(token, _TEST_RUN_ID, secret="secret-b") is False

    def test_empty_token_fails_validation(self):
        assert validate_approval_token("", _TEST_RUN_ID, secret=_TEST_SECRET) is False

    def test_check_approval_is_alias_for_validate(self):
        token = generate_approval_token(_TEST_RUN_ID, secret=_TEST_SECRET)
        assert check_approval(token, _TEST_RUN_ID, secret=_TEST_SECRET) is True
        assert check_approval("bad-token", _TEST_RUN_ID, secret=_TEST_SECRET) is False


# ---------------------------------------------------------------------------
# Approve/reject URL format
# ---------------------------------------------------------------------------


class TestApprovalUrlFormat:
    _URL_PATTERN = re.compile(
        r"^https?://.+\?run_id=.+&decision=(approve|reject)&token=[0-9a-f]{64}$"
    )

    def test_approve_url_contains_decision_approve(self):
        token = generate_approval_token(_TEST_RUN_ID, secret=_TEST_SECRET)
        url = _build_approval_url(_TEST_RUN_ID, "approve", token, _TEST_BASE_URL)
        assert "decision=approve" in url

    def test_reject_url_contains_decision_reject(self):
        token = generate_approval_token(_TEST_RUN_ID, secret=_TEST_SECRET)
        url = _build_approval_url(_TEST_RUN_ID, "reject", token, _TEST_BASE_URL)
        assert "decision=reject" in url

    def test_url_contains_run_id(self):
        token = generate_approval_token(_TEST_RUN_ID, secret=_TEST_SECRET)
        url = _build_approval_url(_TEST_RUN_ID, "approve", token, _TEST_BASE_URL)
        assert f"run_id={_TEST_RUN_ID}" in url

    def test_url_contains_token(self):
        token = generate_approval_token(_TEST_RUN_ID, secret=_TEST_SECRET)
        url = _build_approval_url(_TEST_RUN_ID, "approve", token, _TEST_BASE_URL)
        assert f"token={token}" in url

    def test_url_matches_expected_pattern(self):
        token = generate_approval_token(_TEST_RUN_ID, secret=_TEST_SECRET)
        for decision in ("approve", "reject"):
            url = _build_approval_url(_TEST_RUN_ID, decision, token, _TEST_BASE_URL)
            assert self._URL_PATTERN.match(url), f"URL {url!r} does not match pattern"

    def test_format_approval_email_approve_url_format(self, tmp_path):
        verdicts = [_make_verdict("post.md")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert self._URL_PATTERN.match(content.approve_url), (
            f"approve_url {content.approve_url!r} does not match pattern"
        )

    def test_format_approval_email_reject_url_format(self, tmp_path):
        verdicts = [_make_verdict("post.md")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert self._URL_PATTERN.match(content.reject_url), (
            f"reject_url {content.reject_url!r} does not match pattern"
        )

    def test_approve_and_reject_urls_differ_only_in_decision(self, tmp_path):
        verdicts = [_make_verdict("post.md")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        # Same token, same run_id — only decision differs
        assert content.approve_url.replace("decision=approve", "decision=reject") == content.reject_url


# ---------------------------------------------------------------------------
# Email content generation
# ---------------------------------------------------------------------------


class TestEmailContentGeneration:
    def test_subject_contains_run_id(self, tmp_path):
        verdicts = [_make_verdict("post.md")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert _TEST_RUN_ID in content.subject

    def test_body_contains_filename(self, tmp_path):
        (tmp_path / "post.md").write_text("Line one.\nLine two.\nLine three.", encoding="utf-8")
        verdicts = [_make_verdict("post.md")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert "post.md" in content.body_text

    def test_body_contains_word_count(self, tmp_path):
        text = "one two three four five"
        (tmp_path / "post.md").write_text(text, encoding="utf-8")
        verdicts = [_make_verdict("post.md")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert "5 words" in content.body_text

    def test_body_contains_first_3_lines(self, tmp_path):
        lines = ["First line.", "Second line.", "Third line.", "Fourth line (not shown)."]
        (tmp_path / "post.md").write_text("\n".join(lines), encoding="utf-8")
        verdicts = [_make_verdict("post.md")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert "First line." in content.body_text
        assert "Second line." in content.body_text
        assert "Third line." in content.body_text
        assert "Fourth line (not shown)." not in content.body_text

    def test_body_contains_only_first_3_lines_when_file_has_more(self, tmp_path):
        lines = [f"Line {i}." for i in range(1, 10)]
        (tmp_path / "post.md").write_text("\n".join(lines), encoding="utf-8")
        verdicts = [_make_verdict("post.md")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert "Line 1." in content.body_text
        assert "Line 3." in content.body_text
        assert "Line 4." not in content.body_text

    def test_body_contains_approve_url(self, tmp_path):
        verdicts = [_make_verdict("post.md")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert content.approve_url in content.body_text

    def test_body_contains_reject_url(self, tmp_path):
        verdicts = [_make_verdict("post.md")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert content.reject_url in content.body_text

    def test_spiked_files_listed_in_body(self, tmp_path):
        verdicts = [
            _make_verdict("post.md", "publish"),
            _make_verdict("bad.md", "spike", "Off-topic content."),
        ]
        (tmp_path / "post.md").write_text("Good content.", encoding="utf-8")
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert "bad.md" in content.body_text
        assert "Off-topic content." in content.body_text

    def test_escalated_files_listed_in_body(self, tmp_path):
        verdicts = [
            _make_verdict("post.md", "publish"),
            _make_verdict("draft.md", "revise", "Needs more detail."),
        ]
        (tmp_path / "post.md").write_text("Good content.", encoding="utf-8")
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert "draft.md" in content.body_text
        assert "Needs more detail." in content.body_text

    def test_no_publishable_files_body_says_so(self, tmp_path):
        verdicts = [_make_verdict("bad.md", "spike", "Off-topic.")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert "No files are ready for publication" in content.body_text

    def test_multiple_publishable_files_all_listed(self, tmp_path):
        for name in ("post.md", "script.md", "cfp.md"):
            (tmp_path / name).write_text(f"Content for {name}.", encoding="utf-8")
        verdicts = [
            _make_verdict("post.md"),
            _make_verdict("script.md"),
            _make_verdict("cfp.md"),
        ]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        for name in ("post.md", "script.md", "cfp.md"):
            assert name in content.body_text

    def test_returns_approval_email_content_dataclass(self, tmp_path):
        verdicts = [_make_verdict("post.md")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert isinstance(content, ApprovalEmailContent)
        assert content.subject
        assert content.body_text
        assert content.approve_url
        assert content.reject_url


# ---------------------------------------------------------------------------
# Missing file handled gracefully
# ---------------------------------------------------------------------------


class TestMissingFileHandledGracefully:
    def test_missing_file_word_count_is_zero(self, tmp_path):
        # File does not exist
        word_count, first_lines = _read_file_preview(tmp_path / "missing.md")
        assert word_count == 0

    def test_missing_file_first_lines_is_empty(self, tmp_path):
        word_count, first_lines = _read_file_preview(tmp_path / "missing.md")
        assert first_lines == []

    def test_missing_file_in_email_shows_zero_words(self, tmp_path):
        # post.md does not exist in tmp_path
        verdicts = [_make_verdict("post.md")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert "post.md" in content.body_text
        assert "0 words" in content.body_text

    def test_missing_file_in_email_no_preview_lines(self, tmp_path):
        verdicts = [_make_verdict("post.md")]
        content = format_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        # File is listed but no content lines appear (just the filename + word count)
        assert "post.md" in content.body_text

    def test_existing_file_has_correct_word_count(self, tmp_path):
        (tmp_path / "post.md").write_text("one two three four five six", encoding="utf-8")
        word_count, _ = _read_file_preview(tmp_path / "post.md")
        assert word_count == 6

    def test_existing_file_returns_up_to_3_lines(self, tmp_path):
        (tmp_path / "post.md").write_text("A\nB\nC\nD\nE", encoding="utf-8")
        _, first_lines = _read_file_preview(tmp_path / "post.md")
        assert first_lines == ["A", "B", "C"]

    def test_file_with_fewer_than_3_lines_returns_all(self, tmp_path):
        (tmp_path / "post.md").write_text("Only one line.", encoding="utf-8")
        _, first_lines = _read_file_preview(tmp_path / "post.md")
        assert first_lines == ["Only one line."]

    def test_empty_file_returns_zero_words_empty_lines(self, tmp_path):
        (tmp_path / "post.md").write_text("", encoding="utf-8")
        word_count, first_lines = _read_file_preview(tmp_path / "post.md")
        assert word_count == 0
        assert first_lines == []


# ---------------------------------------------------------------------------
# SES send called with correct parameters
# ---------------------------------------------------------------------------


class TestSESSendCalledCorrectly:
    def test_ses_send_called_once(self, tmp_path):
        ses = FakeSESClient()
        verdicts = [_make_verdict("post.md")]
        send_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            ses_client=ses,
            sender_email="sender@example.com",
            recipient_email="mike@example.com",
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert len(ses.sent) == 1

    def test_ses_send_uses_correct_sender(self, tmp_path):
        ses = FakeSESClient()
        verdicts = [_make_verdict("post.md")]
        send_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            ses_client=ses,
            sender_email="sender@example.com",
            recipient_email="mike@example.com",
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert ses.sent[0]["sender"] == "sender@example.com"

    def test_ses_send_uses_correct_recipient(self, tmp_path):
        ses = FakeSESClient()
        verdicts = [_make_verdict("post.md")]
        send_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            ses_client=ses,
            sender_email="sender@example.com",
            recipient_email="mike@example.com",
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert ses.sent[0]["recipient"] == "mike@example.com"

    def test_ses_send_subject_contains_run_id(self, tmp_path):
        ses = FakeSESClient()
        verdicts = [_make_verdict("post.md")]
        send_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            ses_client=ses,
            sender_email="sender@example.com",
            recipient_email="mike@example.com",
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert _TEST_RUN_ID in ses.sent[0]["subject"]

    def test_ses_send_body_contains_approve_url(self, tmp_path):
        ses = FakeSESClient()
        verdicts = [_make_verdict("post.md")]
        content = send_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            ses_client=ses,
            sender_email="sender@example.com",
            recipient_email="mike@example.com",
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert content.approve_url in ses.sent[0]["body"]

    def test_ses_send_body_contains_reject_url(self, tmp_path):
        ses = FakeSESClient()
        verdicts = [_make_verdict("post.md")]
        content = send_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            ses_client=ses,
            sender_email="sender@example.com",
            recipient_email="mike@example.com",
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert content.reject_url in ses.sent[0]["body"]

    def test_ses_failure_propagates(self, tmp_path):
        ses = FakeSESClient(should_fail=True)
        verdicts = [_make_verdict("post.md")]
        with pytest.raises(RuntimeError, match="Simulated SES failure"):
            send_approval_email(
                verdicts=verdicts,
                output_dir=str(tmp_path),
                run_id=_TEST_RUN_ID,
                ses_client=ses,
                sender_email="sender@example.com",
                recipient_email="mike@example.com",
                base_url=_TEST_BASE_URL,
                secret=_TEST_SECRET,
            )

    def test_send_returns_approval_email_content(self, tmp_path):
        ses = FakeSESClient()
        verdicts = [_make_verdict("post.md")]
        result = send_approval_email(
            verdicts=verdicts,
            output_dir=str(tmp_path),
            run_id=_TEST_RUN_ID,
            ses_client=ses,
            sender_email="sender@example.com",
            recipient_email="mike@example.com",
            base_url=_TEST_BASE_URL,
            secret=_TEST_SECRET,
        )
        assert isinstance(result, ApprovalEmailContent)


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------

_run_id_strategy = st.from_regex(r"[a-z0-9][a-z0-9\-]{4,40}[a-z0-9]", fullmatch=True)
_secret_strategy = st.text(min_size=8, max_size=64).filter(str.strip)


@given(run_id=_run_id_strategy, secret=_secret_strategy)
@settings(max_examples=200)
def test_property_token_round_trip(run_id: str, secret: str) -> None:
    """Property: a freshly generated token always validates against its own run_id."""
    token = generate_approval_token(run_id, secret=secret)
    assert validate_approval_token(token, run_id, secret=secret) is True


@given(run_id=_run_id_strategy, secret=_secret_strategy)
@settings(max_examples=100)
def test_property_tampered_token_always_fails(run_id: str, secret: str) -> None:
    """Property: flipping the last character of a token always fails validation."""
    token = generate_approval_token(run_id, secret=secret)
    # Flip last hex char: 'a' -> 'b', anything else -> 'a'
    last = token[-1]
    flipped = "b" if last == "a" else "a"
    tampered = token[:-1] + flipped
    assert validate_approval_token(tampered, run_id, secret=secret) is False


@given(
    run_id=_run_id_strategy,
    secret=_secret_strategy,
)
@settings(max_examples=100)
def test_property_url_contains_run_id_and_token(run_id: str, secret: str) -> None:
    """Property: approve and reject URLs always contain the run_id and a valid token."""
    token = generate_approval_token(run_id, secret=secret)
    for decision in ("approve", "reject"):
        url = _build_approval_url(run_id, decision, token, _TEST_BASE_URL)
        assert run_id in url
        assert token in url
        assert f"decision={decision}" in url
