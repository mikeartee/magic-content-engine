"""Tests for the embargo release check module.

Covers: HeldItemMemoryProtocol, SESNotifierProtocol, format_released_items,
check_embargo_releases (all paths), and _parse_selection.

Requirements: REQ-031.1–REQ-031.6, REQ-032.1–REQ-032.5
"""

from __future__ import annotations

from datetime import date

import pytest

from magic_content_engine.embargo import (
    HeldItemMemoryProtocol,
    SESNotifierProtocol,
    check_embargo_releases,
    format_released_items,
    _parse_selection,
)
from magic_content_engine.errors import ErrorCollector
from magic_content_engine.models import HeldItem


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeMemory:
    """In-memory fake implementing HeldItemMemoryProtocol."""

    def __init__(self, items: list[HeldItem] | None = None) -> None:
        self._items: list[HeldItem] = list(items) if items else []
        self.removed: list[HeldItem] = []

    def load_held_items(self) -> list[HeldItem]:
        return list(self._items)

    def save_held_item(self, item: HeldItem) -> None:
        self._items.append(item)

    def remove_held_item(self, item: HeldItem) -> None:
        self.removed.append(item)
        self._items = [i for i in self._items if i is not item]


class FakeSES:
    """Records SES calls. Optionally raises on specific filenames."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.sent: list[HeldItem] = []
        self._fail_on = fail_on or set()

    def send_embargo_release(self, item: HeldItem) -> None:
        if item.filename in self._fail_on:
            raise RuntimeError(f"SES delivery failed for {item.filename}")
        self.sent.append(item)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_held(
    filename: str = "post.md",
    release_date: date = date(2025, 7, 10),
    run_date: date = date(2025, 7, 1),
) -> HeldItem:
    return HeldItem(
        filename=filename,
        s3_destination_path=f"output/2025-07-01-slug/{filename}",
        release_date=release_date,
        article_titles=["Article A"],
        run_date=run_date,
        local_file_path=f"./output/held/2025-07-01-slug/{filename}",
    )


# ---------------------------------------------------------------------------
# format_released_items
# ---------------------------------------------------------------------------


class TestFormatReleasedItems:
    def test_empty_list(self):
        result = format_released_items([])
        assert "No embargoed items" in result

    def test_single_item(self):
        item = _make_held()
        result = format_released_items([item])
        assert "[1]" in result
        assert "post.md" in result
        assert "2025-07-01" in result  # run_date
        assert "2025-07-10" in result  # release_date
        assert "./output/held/" in result

    def test_multiple_items(self):
        items = [
            _make_held("post.md", date(2025, 7, 10)),
            _make_held("script.md", date(2025, 7, 12)),
        ]
        result = format_released_items(items)
        assert "[1]" in result
        assert "[2]" in result
        assert "post.md" in result
        assert "script.md" in result


# ---------------------------------------------------------------------------
# _parse_selection
# ---------------------------------------------------------------------------


class TestParseSelection:
    def test_all(self):
        items = [_make_held("a.md"), _make_held("b.md")]
        assert _parse_selection("all", items) == items

    def test_none(self):
        items = [_make_held("a.md")]
        assert _parse_selection("none", items) == []

    def test_empty_string(self):
        items = [_make_held("a.md")]
        assert _parse_selection("", items) == []

    def test_single_index(self):
        items = [_make_held("a.md"), _make_held("b.md")]
        assert _parse_selection("2", items) == [items[1]]

    def test_comma_separated(self):
        items = [_make_held("a.md"), _make_held("b.md"), _make_held("c.md")]
        result = _parse_selection("1,3", items)
        assert result == [items[0], items[2]]

    def test_out_of_range_ignored(self):
        items = [_make_held("a.md")]
        assert _parse_selection("1,5", items) == [items[0]]

    def test_invalid_tokens_ignored(self):
        items = [_make_held("a.md")]
        assert _parse_selection("abc,1", items) == [items[0]]


# ---------------------------------------------------------------------------
# check_embargo_releases — no released items
# ---------------------------------------------------------------------------


class TestCheckEmbargoNoReleased:
    def test_returns_empty_when_no_held_items(self):
        memory = FakeMemory()
        ses = FakeSES()
        collector = ErrorCollector()

        result = check_embargo_releases(
            memory, ses, collector, date(2025, 7, 14)
        )
        assert result == []
        assert ses.sent == []

    def test_returns_empty_when_all_items_future(self):
        future_item = _make_held(release_date=date(2025, 8, 1))
        memory = FakeMemory([future_item])
        ses = FakeSES()
        collector = ErrorCollector()

        result = check_embargo_releases(
            memory, ses, collector, date(2025, 7, 14)
        )
        assert result == []
        assert ses.sent == []
        assert memory.removed == []


# ---------------------------------------------------------------------------
# check_embargo_releases — items released, user selects all
# ---------------------------------------------------------------------------


class TestCheckEmbargoSelectAll:
    def test_all_released_items_confirmed(self):
        item1 = _make_held("post.md", release_date=date(2025, 7, 10))
        item2 = _make_held("script.md", release_date=date(2025, 7, 14))
        memory = FakeMemory([item1, item2])
        ses = FakeSES()
        collector = ErrorCollector()

        result = check_embargo_releases(
            memory, ses, collector, date(2025, 7, 14),
            input_fn=lambda _: "all",
        )

        assert len(result) == 2
        assert result[0].filename == "post.md"
        assert result[1].filename == "script.md"
        # SES sent for both
        assert len(ses.sent) == 2
        # Both removed from memory
        assert len(memory.removed) == 2
        assert not collector.has_errors


# ---------------------------------------------------------------------------
# check_embargo_releases — user selects none
# ---------------------------------------------------------------------------


class TestCheckEmbargoSelectNone:
    def test_none_confirmed(self):
        item = _make_held("post.md", release_date=date(2025, 7, 10))
        memory = FakeMemory([item])
        ses = FakeSES()
        collector = ErrorCollector()

        result = check_embargo_releases(
            memory, ses, collector, date(2025, 7, 14),
            input_fn=lambda _: "none",
        )

        assert result == []
        # SES still sent (notification happens before confirmation)
        assert len(ses.sent) == 1
        # Nothing removed from memory
        assert memory.removed == []


# ---------------------------------------------------------------------------
# check_embargo_releases — user selects specific items
# ---------------------------------------------------------------------------


class TestCheckEmbargoSelectSpecific:
    def test_partial_selection(self):
        item1 = _make_held("post.md", release_date=date(2025, 7, 10))
        item2 = _make_held("script.md", release_date=date(2025, 7, 12))
        item3 = _make_held("cfp.md", release_date=date(2025, 7, 14))
        memory = FakeMemory([item1, item2, item3])
        ses = FakeSES()
        collector = ErrorCollector()

        result = check_embargo_releases(
            memory, ses, collector, date(2025, 7, 14),
            input_fn=lambda _: "1,3",
        )

        assert len(result) == 2
        assert result[0].filename == "post.md"
        assert result[1].filename == "cfp.md"
        # All 3 got SES notifications
        assert len(ses.sent) == 3
        # Only 2 removed from memory
        assert len(memory.removed) == 2


# ---------------------------------------------------------------------------
# check_embargo_releases — SES failure logged, run continues
# ---------------------------------------------------------------------------


class TestCheckEmbargoSESFailure:
    def test_ses_failure_logged_and_continues(self):
        item1 = _make_held("post.md", release_date=date(2025, 7, 10))
        item2 = _make_held("script.md", release_date=date(2025, 7, 12))
        memory = FakeMemory([item1, item2])
        ses = FakeSES(fail_on={"post.md"})
        collector = ErrorCollector()

        result = check_embargo_releases(
            memory, ses, collector, date(2025, 7, 14),
            input_fn=lambda _: "all",
        )

        # Both items still confirmed despite SES failure on first
        assert len(result) == 2
        # SES succeeded only for script.md
        assert len(ses.sent) == 1
        assert ses.sent[0].filename == "script.md"
        # Error collector has the SES failure
        assert collector.has_errors
        assert len(collector.errors) == 1
        assert "post.md" in collector.errors[0].target


# ---------------------------------------------------------------------------
# check_embargo_releases — mix of released and future items
# ---------------------------------------------------------------------------


class TestCheckEmbargoMixed:
    def test_only_released_items_shown(self):
        released = _make_held("post.md", release_date=date(2025, 7, 10))
        future = _make_held("script.md", release_date=date(2025, 8, 1))
        memory = FakeMemory([released, future])
        ses = FakeSES()
        collector = ErrorCollector()

        result = check_embargo_releases(
            memory, ses, collector, date(2025, 7, 14),
            input_fn=lambda _: "all",
        )

        # Only the released item is returned
        assert len(result) == 1
        assert result[0].filename == "post.md"
        # SES sent only for released item
        assert len(ses.sent) == 1
        # Only released item removed
        assert len(memory.removed) == 1


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_fake_memory_is_protocol_compliant(self):
        assert isinstance(FakeMemory(), HeldItemMemoryProtocol)

    def test_fake_ses_is_protocol_compliant(self):
        assert isinstance(FakeSES(), SESNotifierProtocol)
