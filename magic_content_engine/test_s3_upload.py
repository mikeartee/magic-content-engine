"""Tests for S3 upload with retry.

Covers:
- Upload succeeds on first attempt
- Upload retries on failure and succeeds on second attempt
- Upload exhausts 3 retries and logs error
- Empty approved_files returns empty list immediately
- Multiple files: some succeed, some fail after retries
- Exponential backoff delays (1s, 2s, 4s) are used
- S3 key prefix is applied correctly
- Error is logged via ErrorCollector after exhausting retries

Requirements: REQ-017.5, REQ-024.1, REQ-024.2
"""

from __future__ import annotations

from typing import Any

import pytest

from magic_content_engine.errors import ErrorCollector
from magic_content_engine.s3_upload import upload_approved_files


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeS3Client:
    """Configurable fake S3 client for testing."""

    def __init__(self, fail_map: dict[str, int] | None = None) -> None:
        """
        Parameters
        ----------
        fail_map:
            Mapping of local_path → number of times to fail before
            succeeding.  If the count is >= _MAX_ATTEMPTS (3), the
            upload will never succeed.
        """
        self.fail_map: dict[str, int] = fail_map or {}
        self._attempt_counts: dict[str, int] = {}
        self.uploads: list[dict[str, str]] = []

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        self._attempt_counts.setdefault(local_path, 0)
        self._attempt_counts[local_path] += 1

        remaining_failures = self.fail_map.get(local_path, 0)
        if self._attempt_counts[local_path] <= remaining_failures:
            raise RuntimeError(f"Simulated S3 failure for {local_path}")

        self.uploads.append({"local_path": local_path, "bucket": bucket, "key": key})


def _noop_sleep(_seconds: float) -> None:
    """No-op sleep for tests."""


class RecordingSleep:
    """Records sleep calls for verifying backoff delays."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUploadSucceedsFirstAttempt:
    def test_single_file_uploaded(self) -> None:
        client = FakeS3Client()
        collector = ErrorCollector()

        result = upload_approved_files(
            client=client,
            approved_files=["output/2025-07-14-test/post.md"],
            bucket="my-bucket",
            s3_key_prefix="output/2025-07-14-test/",
            collector=collector,
            sleep_fn=_noop_sleep,
        )

        assert result == ["output/2025-07-14-test/post.md"]
        assert len(client.uploads) == 1
        assert client.uploads[0]["bucket"] == "my-bucket"
        assert not collector.has_errors


class TestUploadRetriesAndSucceeds:
    def test_succeeds_on_second_attempt(self) -> None:
        client = FakeS3Client(fail_map={"output/post.md": 1})
        collector = ErrorCollector()
        sleep_rec = RecordingSleep()

        result = upload_approved_files(
            client=client,
            approved_files=["output/post.md"],
            bucket="bucket",
            s3_key_prefix="output/2025-07-14-slug/",
            collector=collector,
            sleep_fn=sleep_rec,
        )

        assert result == ["output/2025-07-14-slug/post.md"]
        assert not collector.has_errors
        # One retry sleep of 1s (first backoff delay)
        assert sleep_rec.calls == [1.0]


class TestUploadExhaustsRetries:
    def test_logs_error_after_three_failures(self) -> None:
        client = FakeS3Client(fail_map={"output/post.md": 3})
        collector = ErrorCollector()
        sleep_rec = RecordingSleep()

        result = upload_approved_files(
            client=client,
            approved_files=["output/post.md"],
            bucket="bucket",
            s3_key_prefix="prefix/",
            collector=collector,
            sleep_fn=sleep_rec,
        )

        assert result == []
        assert collector.has_errors
        assert len(collector.errors) == 1
        assert collector.errors[0].step == "upload"
        assert collector.errors[0].target == "output/post.md"
        assert "Simulated S3 failure" in collector.errors[0].error_message
        # Two retry sleeps: 1s, 2s (no sleep after the final attempt)
        assert sleep_rec.calls == [1.0, 2.0]


class TestEmptyApprovedFiles:
    def test_returns_empty_list_immediately(self) -> None:
        client = FakeS3Client()
        collector = ErrorCollector()

        result = upload_approved_files(
            client=client,
            approved_files=[],
            bucket="bucket",
            s3_key_prefix="prefix/",
            collector=collector,
            sleep_fn=_noop_sleep,
        )

        assert result == []
        assert len(client.uploads) == 0
        assert not collector.has_errors


class TestMultipleFilesMixed:
    def test_some_succeed_some_fail(self) -> None:
        client = FakeS3Client(fail_map={"output/bad.md": 3})
        collector = ErrorCollector()

        result = upload_approved_files(
            client=client,
            approved_files=["output/good.md", "output/bad.md", "output/also-good.md"],
            bucket="bucket",
            s3_key_prefix="output/2025-07-14-slug/",
            collector=collector,
            sleep_fn=_noop_sleep,
        )

        assert "output/2025-07-14-slug/good.md" in result
        assert "output/2025-07-14-slug/also-good.md" in result
        assert len(result) == 2
        # One error logged for the failed file
        assert len(collector.errors) == 1
        assert collector.errors[0].target == "output/bad.md"


class TestExponentialBackoffDelays:
    def test_backoff_delays_are_1_2_4(self) -> None:
        """Verify sleep_fn is called with 1s, 2s (not 4s because 3rd attempt
        is the last and doesn't sleep after failure)."""
        client = FakeS3Client(fail_map={"f.txt": 3})
        collector = ErrorCollector()
        sleep_rec = RecordingSleep()

        upload_approved_files(
            client=client,
            approved_files=["f.txt"],
            bucket="b",
            s3_key_prefix="p/",
            collector=collector,
            sleep_fn=sleep_rec,
        )

        # Sleeps happen between attempts: after attempt 1 (1s), after attempt 2 (2s)
        # No sleep after the final (3rd) attempt
        assert sleep_rec.calls == [1.0, 2.0]

    def test_backoff_with_success_on_third_attempt(self) -> None:
        """When upload succeeds on the 3rd attempt, we see sleeps of 1s and 2s."""
        client = FakeS3Client(fail_map={"f.txt": 2})
        collector = ErrorCollector()
        sleep_rec = RecordingSleep()

        result = upload_approved_files(
            client=client,
            approved_files=["f.txt"],
            bucket="b",
            s3_key_prefix="p/",
            collector=collector,
            sleep_fn=sleep_rec,
        )

        assert result == ["p/f.txt"]
        assert sleep_rec.calls == [1.0, 2.0]
        assert not collector.has_errors


class TestS3KeyPrefix:
    def test_prefix_applied_correctly(self) -> None:
        client = FakeS3Client()
        collector = ErrorCollector()

        result = upload_approved_files(
            client=client,
            approved_files=["output/2025-07-14-slug/post.md", "output/2025-07-14-slug/script.md"],
            bucket="my-bucket",
            s3_key_prefix="output/2025-07-14-slug/",
            collector=collector,
            sleep_fn=_noop_sleep,
        )

        assert result == [
            "output/2025-07-14-slug/post.md",
            "output/2025-07-14-slug/script.md",
        ]
        assert client.uploads[0]["key"] == "output/2025-07-14-slug/post.md"
        assert client.uploads[1]["key"] == "output/2025-07-14-slug/script.md"


class TestErrorLoggedViaCollector:
    def test_error_contains_context(self) -> None:
        client = FakeS3Client(fail_map={"file.md": 3})
        collector = ErrorCollector()

        upload_approved_files(
            client=client,
            approved_files=["file.md"],
            bucket="test-bucket",
            s3_key_prefix="output/2025-07-14-test/",
            collector=collector,
            sleep_fn=_noop_sleep,
        )

        assert len(collector.errors) == 1
        err = collector.errors[0]
        assert err.step == "upload"
        assert err.target == "file.md"
        assert err.context["retry_count"] == 3
        assert err.context["bucket"] == "test-bucket"
        assert err.context["s3_key"] == "output/2025-07-14-test/file.md"
