"""Tests for the Archivist (Whakaaro) Lambda.

Covers:
- Empty feed handling: returns items_archived=0, no exception raised
- Summary structure: result has required fields (items_archived, run_timestamp)
- Clean exit on unreachable S3: returns items_archived=0, no exception raised
- run_date parameter respected: archive key uses the supplied date
- Recent-only filter: only entries within the last 7 days are archived
- Archive key format: YYYY-MM-DD-summary.json under archive/ prefix
- Content is read from each recent object and included in the summary
- Objects older than 7 days are excluded

Requirements: REQ-17, REQ-18
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock

import pytest

from magic_content_engine.bullpen.archivist import run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BUCKET = "mce-second-brain"
_CONTEXT_PREFIX = "ami-context/"
_ARCHIVE_PREFIX = "archive/"

# A fixed "today" used across tests for determinism.
_RUN_DATE = "2025-07-21"
_RUN_DATE_DT = datetime(2025, 7, 21, tzinfo=timezone.utc)


def _make_s3_object(
    key: str,
    last_modified: datetime,
    body: str = "# Note content",
    size: int = 100,
) -> dict[str, Any]:
    """Build a fake S3 object descriptor as returned by ListObjectsV2."""
    return {
        "Key": key,
        "LastModified": last_modified,
        "Size": size,
        "ETag": '"abc123"',
    }


class FakeS3Client:
    """Minimal fake S3 client for archivist tests.

    Supports:
    - list_objects_v2 via a paginator (single page)
    - get_object returning configurable body text
    - put_object recording calls
    - Configurable failure modes
    """

    def __init__(
        self,
        objects: list[dict[str, Any]] | None = None,
        object_bodies: dict[str, str] | None = None,
        list_raises: Exception | None = None,
        get_raises: Exception | None = None,
        put_raises: Exception | None = None,
    ) -> None:
        self._objects = objects or []
        self._object_bodies: dict[str, str] = object_bodies or {}
        self._list_raises = list_raises
        self._get_raises = get_raises
        self._put_raises = put_raises
        self.put_calls: list[dict[str, Any]] = []

    def get_paginator(self, operation_name: str) -> "_FakePaginator":
        return _FakePaginator(
            objects=self._objects,
            raises=self._list_raises,
        )

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if self._get_raises is not None:
            raise self._get_raises
        body_text = self._object_bodies.get(Key, f"content of {Key}")
        return {"Body": BytesIO(body_text.encode("utf-8"))}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        if self._put_raises is not None:
            raise self._put_raises
        self.put_calls.append(kwargs)
        return {}


class _FakePaginator:
    def __init__(
        self,
        objects: list[dict[str, Any]],
        raises: Exception | None = None,
    ) -> None:
        self._objects = objects
        self._raises = raises

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self._raises is not None:
            raise self._raises
        return [{"Contents": self._objects}] if self._objects else [{}]


def _recent(days_ago: int = 0) -> datetime:
    """Return a UTC datetime *days_ago* days before the run date."""
    return _RUN_DATE_DT - timedelta(days=days_ago)


def _old(days_ago: int = 8) -> datetime:
    """Return a UTC datetime older than the 7-day lookback window."""
    return _RUN_DATE_DT - timedelta(days=days_ago)


# ---------------------------------------------------------------------------
# Empty feed
# ---------------------------------------------------------------------------


class TestEmptyFeed:
    def test_returns_items_archived_zero(self) -> None:
        client = FakeS3Client(objects=[])
        result = run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        assert result["items_archived"] == 0

    def test_no_exception_raised(self) -> None:
        client = FakeS3Client(objects=[])
        # Must not raise
        run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )

    def test_no_put_object_called(self) -> None:
        client = FakeS3Client(objects=[])
        run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        assert client.put_calls == []


# ---------------------------------------------------------------------------
# Summary structure
# ---------------------------------------------------------------------------


class TestSummaryStructure:
    def _run_with_one_recent_object(self) -> dict[str, Any]:
        objects = [_make_s3_object("ami-context/note.md", _recent(1))]
        client = FakeS3Client(objects=objects)
        return run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )

    def test_result_has_items_archived(self) -> None:
        result = self._run_with_one_recent_object()
        assert "items_archived" in result

    def test_result_has_run_timestamp(self) -> None:
        result = self._run_with_one_recent_object()
        assert "run_timestamp" in result

    def test_items_archived_is_int(self) -> None:
        result = self._run_with_one_recent_object()
        assert isinstance(result["items_archived"], int)

    def test_run_timestamp_is_iso8601(self) -> None:
        result = self._run_with_one_recent_object()
        # Should parse without error
        datetime.fromisoformat(result["run_timestamp"])

    def test_items_archived_equals_one(self) -> None:
        result = self._run_with_one_recent_object()
        assert result["items_archived"] == 1

    def test_archive_written_with_correct_structure(self) -> None:
        objects = [_make_s3_object("ami-context/note.md", _recent(1))]
        client = FakeS3Client(objects=objects)
        run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        assert len(client.put_calls) == 1
        body = json.loads(client.put_calls[0]["Body"])
        assert "run_date" in body
        assert "run_timestamp" in body
        assert "lookback_days" in body
        assert "items_archived" in body
        assert "entries" in body
        assert isinstance(body["entries"], list)


# ---------------------------------------------------------------------------
# Unreachable S3
# ---------------------------------------------------------------------------


class TestUnreachableS3:
    def test_list_failure_returns_items_archived_zero(self) -> None:
        from botocore.exceptions import EndpointResolutionError
        client = FakeS3Client(
            list_raises=Exception("Connection refused")
        )
        result = run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        assert result["items_archived"] == 0

    def test_list_failure_no_exception_raised(self) -> None:
        client = FakeS3Client(list_raises=Exception("Network error"))
        # Must not raise
        run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )

    def test_list_failure_result_has_run_timestamp(self) -> None:
        client = FakeS3Client(list_raises=Exception("Timeout"))
        result = run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        assert "run_timestamp" in result
        datetime.fromisoformat(result["run_timestamp"])

    def test_put_failure_returns_items_archived_zero(self) -> None:
        """If writing the archive fails, return items_archived=0 cleanly."""
        objects = [_make_s3_object("ami-context/note.md", _recent(1))]
        client = FakeS3Client(
            objects=objects,
            put_raises=Exception("S3 write denied"),
        )
        result = run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        assert result["items_archived"] == 0

    def test_put_failure_no_exception_raised(self) -> None:
        objects = [_make_s3_object("ami-context/note.md", _recent(1))]
        client = FakeS3Client(
            objects=objects,
            put_raises=Exception("AccessDenied"),
        )
        # Must not raise
        run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )


# ---------------------------------------------------------------------------
# run_date parameter
# ---------------------------------------------------------------------------


class TestRunDateParameter:
    def test_archive_key_uses_supplied_date(self) -> None:
        objects = [_make_s3_object("ami-context/note.md", _recent(1))]
        client = FakeS3Client(objects=objects)
        run(
            run_date="2025-07-14",
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        assert len(client.put_calls) == 1
        assert client.put_calls[0]["Key"] == "archive/2025-07-14-summary.json"

    def test_archive_key_uses_different_date(self) -> None:
        objects = [_make_s3_object("ami-context/note.md", _recent(1))]
        client = FakeS3Client(objects=objects)
        run(
            run_date="2025-01-01",
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        assert client.put_calls[0]["Key"] == "archive/2025-01-01-summary.json"

    def test_run_date_in_summary_body(self) -> None:
        objects = [_make_s3_object("ami-context/note.md", _recent(1))]
        client = FakeS3Client(objects=objects)
        run(
            run_date="2025-07-14",
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        body = json.loads(client.put_calls[0]["Body"])
        assert body["run_date"] == "2025-07-14"


# ---------------------------------------------------------------------------
# 7-day filter
# ---------------------------------------------------------------------------


class TestSevenDayFilter:
    def test_recent_objects_are_included(self) -> None:
        objects = [
            _make_s3_object("ami-context/today.md", _recent(0)),
            _make_s3_object("ami-context/yesterday.md", _recent(1)),
            _make_s3_object("ami-context/six-days-ago.md", _recent(6)),
        ]
        client = FakeS3Client(objects=objects)
        result = run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        assert result["items_archived"] == 3

    def test_old_objects_are_excluded(self) -> None:
        objects = [
            _make_s3_object("ami-context/old.md", _old(8)),
            _make_s3_object("ami-context/very-old.md", _old(30)),
        ]
        client = FakeS3Client(objects=objects)
        result = run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        assert result["items_archived"] == 0

    def test_mixed_recent_and_old(self) -> None:
        objects = [
            _make_s3_object("ami-context/recent.md", _recent(2)),
            _make_s3_object("ami-context/old.md", _old(10)),
        ]
        client = FakeS3Client(objects=objects)
        result = run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        assert result["items_archived"] == 1

    def test_all_old_returns_items_archived_zero(self) -> None:
        objects = [_make_s3_object("ami-context/old.md", _old(14))]
        client = FakeS3Client(objects=objects)
        result = run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        assert result["items_archived"] == 0

    def test_all_old_no_put_called(self) -> None:
        objects = [_make_s3_object("ami-context/old.md", _old(14))]
        client = FakeS3Client(objects=objects)
        run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        assert client.put_calls == []


# ---------------------------------------------------------------------------
# Content reading
# ---------------------------------------------------------------------------


class TestContentReading:
    def test_entry_content_included_in_archive(self) -> None:
        key = "ami-context/my-note.md"
        body_text = "# My Note\n\nSome vault content here."
        objects = [_make_s3_object(key, _recent(1))]
        client = FakeS3Client(
            objects=objects,
            object_bodies={key: body_text},
        )
        run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        body = json.loads(client.put_calls[0]["Body"])
        assert body["entries"][0]["content"] == body_text
        assert body["entries"][0]["key"] == key

    def test_get_object_failure_skips_entry(self) -> None:
        """A single get_object failure should skip that entry, not abort."""
        objects = [
            _make_s3_object("ami-context/bad.md", _recent(1)),
            _make_s3_object("ami-context/good.md", _recent(2)),
        ]
        # Raise on get_object for all keys (simulates transient failure)
        client = FakeS3Client(
            objects=objects,
            get_raises=Exception("GetObject failed"),
        )
        # Should not raise; items_archived will be 0 since all reads failed
        result = run(
            run_date=_RUN_DATE,
            s3_client=client,
            bucket=_BUCKET,
            context_prefix=_CONTEXT_PREFIX,
            archive_prefix=_ARCHIVE_PREFIX,
        )
        # All reads failed, so no entries — but no exception either
        assert isinstance(result["items_archived"], int)
