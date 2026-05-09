"""Tests for the Publisher Lambda.

Covers:
- S3 key format correctness (prefix + filename)
- Retry backoff timing (1s, 2s, 4s)
- Per-file failure continuation (remaining files still upload)
- PublicationReport JSON round-trip
- Property-based test: S3 key prefix format invariant

Requirements: REQ-bullpen-12, REQ-bullpen-13, REQ-bullpen-25.5
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from magic_content_engine.bullpen.models import PublicationReport, UploadedFile
from magic_content_engine.bullpen.publisher import publish
from magic_content_engine.errors import ErrorCollector


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeS3Client:
    """Configurable fake S3 client.

    Parameters
    ----------
    fail_map:
        Mapping of local_path → number of times to fail before succeeding.
        If count >= 3 the upload never succeeds.
    """

    def __init__(self, fail_map: dict[str, int] | None = None) -> None:
        self.fail_map: dict[str, int] = fail_map or {}
        self._attempt_counts: dict[str, int] = {}
        self.uploads: list[dict[str, str]] = []

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        self._attempt_counts.setdefault(local_path, 0)
        self._attempt_counts[local_path] += 1
        remaining = self.fail_map.get(local_path, 0)
        if self._attempt_counts[local_path] <= remaining:
            raise RuntimeError(f"Simulated S3 failure for {local_path}")
        self.uploads.append({"local_path": local_path, "bucket": bucket, "key": key})


class FakeSESClient:
    """Fake SES client that records sent emails."""

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


class RecordingSleep:
    """Records sleep calls for verifying backoff delays."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _noop_sleep(_: float) -> None:
    pass


# ---------------------------------------------------------------------------
# S3 key format correctness
# ---------------------------------------------------------------------------


class TestS3KeyFormat:
    def test_key_is_prefix_plus_filename(self) -> None:
        s3 = FakeS3Client()
        ses = FakeSESClient()

        report = publish(
            approved_files=["output/2025-07-14-agentcore/post.md"],
            s3_key_prefix="output/2025-07-14-agentcore/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="sender@example.com",
            recipient_email="mike@example.com",
            sleep_fn=_noop_sleep,
        )

        assert len(report.files_uploaded) == 1
        assert report.files_uploaded[0].s3_key == "output/2025-07-14-agentcore/post.md"

    def test_key_format_matches_pattern(self) -> None:
        """S3 key must match output/YYYY-MM-DD-[slug]/[filename]."""
        s3 = FakeS3Client()
        ses = FakeSESClient()
        pattern = re.compile(r"^output/\d{4}-\d{2}-\d{2}-[a-z0-9-]+/[^/]+$")

        report = publish(
            approved_files=["output/2025-07-14-kiro-launch/script.md"],
            s3_key_prefix="output/2025-07-14-kiro-launch/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="sender@example.com",
            recipient_email="mike@example.com",
            sleep_fn=_noop_sleep,
        )

        for uf in report.files_uploaded:
            assert pattern.match(uf.s3_key), f"Key {uf.s3_key!r} does not match pattern"

    def test_multiple_files_all_get_correct_keys(self) -> None:
        s3 = FakeS3Client()
        ses = FakeSESClient()
        prefix = "output/2025-07-14-test-slug/"

        report = publish(
            approved_files=[
                "output/2025-07-14-test-slug/post.md",
                "output/2025-07-14-test-slug/script.md",
                "output/2025-07-14-test-slug/description.txt",
            ],
            s3_key_prefix=prefix,
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="sender@example.com",
            recipient_email="mike@example.com",
            sleep_fn=_noop_sleep,
        )

        keys = [uf.s3_key for uf in report.files_uploaded]
        assert keys == [
            "output/2025-07-14-test-slug/post.md",
            "output/2025-07-14-test-slug/script.md",
            "output/2025-07-14-test-slug/description.txt",
        ]

    def test_filename_extracted_from_path_with_backslash(self) -> None:
        """Windows-style paths should still produce correct S3 keys."""
        s3 = FakeS3Client()
        ses = FakeSESClient()

        report = publish(
            approved_files=["output\\2025-07-14-test\\post.md"],
            s3_key_prefix="output/2025-07-14-test/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="sender@example.com",
            recipient_email="mike@example.com",
            sleep_fn=_noop_sleep,
        )

        assert report.files_uploaded[0].s3_key == "output/2025-07-14-test/post.md"


# ---------------------------------------------------------------------------
# Retry backoff timing
# ---------------------------------------------------------------------------


class TestRetryBackoffTiming:
    def test_no_sleep_on_first_attempt_success(self) -> None:
        s3 = FakeS3Client()
        ses = FakeSESClient()
        sleep_rec = RecordingSleep()

        publish(
            approved_files=["output/2025-07-14-slug/post.md"],
            s3_key_prefix="output/2025-07-14-slug/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="s@e.com",
            recipient_email="r@e.com",
            sleep_fn=sleep_rec,
        )

        assert sleep_rec.calls == []

    def test_sleep_1s_on_first_retry(self) -> None:
        s3 = FakeS3Client(fail_map={"output/post.md": 1})
        ses = FakeSESClient()
        sleep_rec = RecordingSleep()

        publish(
            approved_files=["output/post.md"],
            s3_key_prefix="output/2025-07-14-slug/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="s@e.com",
            recipient_email="r@e.com",
            sleep_fn=sleep_rec,
        )

        assert sleep_rec.calls == [1.0]

    def test_sleep_1s_2s_on_two_retries(self) -> None:
        s3 = FakeS3Client(fail_map={"output/post.md": 2})
        ses = FakeSESClient()
        sleep_rec = RecordingSleep()

        publish(
            approved_files=["output/post.md"],
            s3_key_prefix="output/2025-07-14-slug/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="s@e.com",
            recipient_email="r@e.com",
            sleep_fn=sleep_rec,
        )

        assert sleep_rec.calls == [1.0, 2.0]

    def test_sleep_1s_2s_on_exhausted_retries(self) -> None:
        """After 3 failures, sleeps are 1s and 2s (no sleep after final attempt)."""
        s3 = FakeS3Client(fail_map={"output/post.md": 3})
        ses = FakeSESClient()
        sleep_rec = RecordingSleep()
        collector = ErrorCollector()

        publish(
            approved_files=["output/post.md"],
            s3_key_prefix="output/2025-07-14-slug/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="s@e.com",
            recipient_email="r@e.com",
            collector=collector,
            sleep_fn=sleep_rec,
        )

        # Sleeps: after attempt 1 (1s), after attempt 2 (2s). No sleep after attempt 3.
        assert sleep_rec.calls == [1.0, 2.0]

    def test_backoff_sequence_is_1_2_4_on_success_at_third(self) -> None:
        """When upload succeeds on the 3rd attempt, sleeps are 1s and 2s."""
        s3 = FakeS3Client(fail_map={"output/post.md": 2})
        ses = FakeSESClient()
        sleep_rec = RecordingSleep()

        report = publish(
            approved_files=["output/post.md"],
            s3_key_prefix="output/2025-07-14-slug/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="s@e.com",
            recipient_email="r@e.com",
            sleep_fn=sleep_rec,
        )

        assert sleep_rec.calls == [1.0, 2.0]
        assert len(report.files_uploaded) == 1


# ---------------------------------------------------------------------------
# Per-file failure continuation
# ---------------------------------------------------------------------------


class TestPerFileFailureContinuation:
    def test_failed_file_skipped_others_continue(self) -> None:
        s3 = FakeS3Client(fail_map={"output/bad.md": 3})
        ses = FakeSESClient()
        collector = ErrorCollector()

        report = publish(
            approved_files=[
                "output/good.md",
                "output/bad.md",
                "output/also-good.md",
            ],
            s3_key_prefix="output/2025-07-14-slug/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="s@e.com",
            recipient_email="r@e.com",
            collector=collector,
            sleep_fn=_noop_sleep,
        )

        uploaded_keys = [uf.s3_key for uf in report.files_uploaded]
        assert "output/2025-07-14-slug/good.md" in uploaded_keys
        assert "output/2025-07-14-slug/also-good.md" in uploaded_keys
        assert len(report.files_uploaded) == 2

    def test_failed_file_logged_in_collector(self) -> None:
        s3 = FakeS3Client(fail_map={"output/bad.md": 3})
        ses = FakeSESClient()
        collector = ErrorCollector()

        publish(
            approved_files=["output/bad.md"],
            s3_key_prefix="output/2025-07-14-slug/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="s@e.com",
            recipient_email="r@e.com",
            collector=collector,
            sleep_fn=_noop_sleep,
        )

        assert collector.has_errors
        assert len(collector.errors) == 1
        err = collector.errors[0]
        assert err.step == "upload"
        assert err.target == "output/bad.md"
        assert err.context["retry_count"] == 3

    def test_all_files_fail_report_has_empty_uploads(self) -> None:
        s3 = FakeS3Client(fail_map={"a.md": 3, "b.md": 3})
        ses = FakeSESClient()
        collector = ErrorCollector()

        report = publish(
            approved_files=["a.md", "b.md"],
            s3_key_prefix="output/2025-07-14-slug/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="s@e.com",
            recipient_email="r@e.com",
            collector=collector,
            sleep_fn=_noop_sleep,
        )

        assert report.files_uploaded == []
        assert len(collector.errors) == 2

    def test_empty_approved_files_returns_empty_report(self) -> None:
        s3 = FakeS3Client()
        ses = FakeSESClient()

        report = publish(
            approved_files=[],
            s3_key_prefix="output/2025-07-14-slug/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="s@e.com",
            recipient_email="r@e.com",
            sleep_fn=_noop_sleep,
        )

        assert report.files_uploaded == []
        assert len(s3.uploads) == 0

    def test_ses_failure_does_not_prevent_report(self) -> None:
        """SES failure is logged but the PublicationReport is still returned."""
        s3 = FakeS3Client()
        ses = FakeSESClient(should_fail=True)
        collector = ErrorCollector()

        report = publish(
            approved_files=["output/post.md"],
            s3_key_prefix="output/2025-07-14-slug/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="s@e.com",
            recipient_email="r@e.com",
            collector=collector,
            sleep_fn=_noop_sleep,
        )

        assert report.email_sent is False
        assert len(report.files_uploaded) == 1
        assert collector.has_errors
        assert collector.errors[0].step == "ses"

    def test_ses_success_sets_email_sent_true(self) -> None:
        s3 = FakeS3Client()
        ses = FakeSESClient()

        report = publish(
            approved_files=["output/post.md"],
            s3_key_prefix="output/2025-07-14-slug/",
            bucket="mce-second-brain",
            s3_client=s3,
            ses_client=ses,
            sender_email="sender@example.com",
            recipient_email="mike@example.com",
            sleep_fn=_noop_sleep,
        )

        assert report.email_sent is True
        assert report.email_recipient == "mike@example.com"
        assert len(ses.sent) == 1


# ---------------------------------------------------------------------------
# PublicationReport JSON round-trip
# ---------------------------------------------------------------------------


class TestPublicationReportJsonRoundTrip:
    def test_round_trip_preserves_all_fields(self) -> None:
        original = PublicationReport(
            files_uploaded=[
                UploadedFile(
                    local_path="output/2025-07-14-slug/post.md",
                    s3_key="output/2025-07-14-slug/post.md",
                ),
                UploadedFile(
                    local_path="output/2025-07-14-slug/script.md",
                    s3_key="output/2025-07-14-slug/script.md",
                ),
            ],
            email_sent=True,
            email_recipient="mike@example.com",
            run_timestamp="2025-07-14T09:00:00+00:00",
        )

        json_str = original.to_json()
        restored = PublicationReport.from_json(json_str)

        assert restored.email_sent == original.email_sent
        assert restored.email_recipient == original.email_recipient
        assert restored.run_timestamp == original.run_timestamp
        assert len(restored.files_uploaded) == len(original.files_uploaded)
        for orig_f, rest_f in zip(original.files_uploaded, restored.files_uploaded):
            assert rest_f.local_path == orig_f.local_path
            assert rest_f.s3_key == orig_f.s3_key

    def test_round_trip_empty_files(self) -> None:
        original = PublicationReport(
            files_uploaded=[],
            email_sent=False,
            email_recipient="mike@example.com",
            run_timestamp="2025-07-14T09:00:00+00:00",
        )

        restored = PublicationReport.from_json(original.to_json())

        assert restored.files_uploaded == []
        assert restored.email_sent is False

    def test_to_dict_is_json_serialisable(self) -> None:
        import json

        report = PublicationReport(
            files_uploaded=[UploadedFile(local_path="a.md", s3_key="output/a.md")],
            email_sent=True,
            email_recipient="r@e.com",
            run_timestamp="2025-07-14T09:00:00+00:00",
        )

        # Should not raise
        serialised = json.dumps(report.to_dict())
        assert "files_uploaded" in serialised
        assert "email_sent" in serialised


# ---------------------------------------------------------------------------
# Property-based test: S3 key prefix format invariant
# ---------------------------------------------------------------------------

# Strategy for valid slugs: lowercase letters, digits, hyphens, no leading/trailing hyphens
_slug_strategy = st.from_regex(r"[a-z0-9][a-z0-9-]{0,20}[a-z0-9]", fullmatch=True)

# Strategy for valid dates: YYYY-MM-DD
_date_strategy = st.dates(
    min_value=__import__("datetime").date(2020, 1, 1),
    max_value=__import__("datetime").date(2030, 12, 31),
)

# Strategy for filenames: simple alphanumeric with extension
_filename_strategy = st.from_regex(r"[a-z][a-z0-9-]{0,20}\.(md|txt|bib)", fullmatch=True)

_S3_KEY_PREFIX_PATTERN = re.compile(r"^output/\d{4}-\d{2}-\d{2}-[a-z0-9-]+/$")
_S3_KEY_FULL_PATTERN = re.compile(r"^output/\d{4}-\d{2}-\d{2}-[a-z0-9-]+/[^/]+$")


@given(
    run_date=_date_strategy,
    slug=_slug_strategy,
    filenames=st.lists(_filename_strategy, min_size=1, max_size=5),
)
@settings(max_examples=100)
def test_s3_key_prefix_format_invariant(
    run_date: __import__("datetime").date,
    slug: str,
    filenames: list[str],
) -> None:
    """For any valid date and slug, all S3 keys must match the expected pattern.

    Property: the S3 key prefix always matches ``^output/\\d{4}-\\d{2}-\\d{2}-[a-z0-9-]+/``
    and each full key matches ``^output/\\d{4}-\\d{2}-\\d{2}-[a-z0-9-]+/[^/]+$``.
    """
    s3_key_prefix = f"output/{run_date.isoformat()}-{slug}/"

    # Verify the prefix itself matches the invariant
    assert _S3_KEY_PREFIX_PATTERN.match(s3_key_prefix), (
        f"Prefix {s3_key_prefix!r} does not match expected pattern"
    )

    s3 = FakeS3Client()
    ses = FakeSESClient()

    # Build local paths that look like real output files
    local_paths = [f"output/{run_date.isoformat()}-{slug}/{fn}" for fn in filenames]

    report = publish(
        approved_files=local_paths,
        s3_key_prefix=s3_key_prefix,
        bucket="mce-second-brain",
        s3_client=s3,
        ses_client=ses,
        sender_email="sender@example.com",
        recipient_email="mike@example.com",
        sleep_fn=_noop_sleep,
    )

    assert len(report.files_uploaded) == len(filenames)
    for uf in report.files_uploaded:
        assert _S3_KEY_FULL_PATTERN.match(uf.s3_key), (
            f"S3 key {uf.s3_key!r} does not match expected pattern"
        )
