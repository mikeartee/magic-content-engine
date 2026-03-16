"""Tests for the screenshot capture service.

Covers:
- CONSOLE_SCREENSHOTS constant validation
- Console screenshot capture (success and partial failure)
- Research screenshot filename generation
- Research screenshot capture (success and partial failure)
- capture_all_screenshots combining results
- ScreenshotCapture defaults
- Error logging via ErrorCollector

Requirements: REQ-016.1–REQ-016.5
"""

from __future__ import annotations

from datetime import date

import pytest

from magic_content_engine.errors import ErrorCollector
from magic_content_engine.models import Article, ScreenshotCapture
from magic_content_engine.screenshots import (
    CONSOLE_SCREENSHOTS,
    BrowserProtocol,
    _source_slug,
    capture_all_screenshots,
    capture_console_screenshots,
    capture_research_screenshots,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeBrowser:
    """A fake browser that returns dummy PNG bytes."""

    def __init__(self, fail_urls: set[str] | None = None) -> None:
        self.fail_urls = fail_urls or set()
        self.calls: list[dict] = []

    def capture(
        self,
        url: str,
        viewport_width: int,
        viewport_height: int,
        wait_seconds: int,
    ) -> bytes:
        self.calls.append(
            {
                "url": url,
                "viewport_width": viewport_width,
                "viewport_height": viewport_height,
                "wait_seconds": wait_seconds,
            }
        )
        if url in self.fail_urls:
            raise RuntimeError(f"Capture failed for {url}")
        return b"\x89PNG fake screenshot data"


def _make_article(
    url: str = "https://example.com/article",
    source: str = "example.com",
    status: str = "confirmed",
) -> Article:
    return Article(
        url=url,
        title="Test Article",
        source=source,
        source_type="primary",
        discovered_date=date(2025, 7, 14),
        status=status,
    )


# ---------------------------------------------------------------------------
# CONSOLE_SCREENSHOTS constant
# ---------------------------------------------------------------------------


class TestConsoleScreenshotsConstant:
    def test_has_exactly_five_entries(self) -> None:
        assert len(CONSOLE_SCREENSHOTS) == 5

    def test_correct_filenames(self) -> None:
        filenames = [fn for fn, _ in CONSOLE_SCREENSHOTS]
        assert filenames == [
            "console-runtime.png",
            "console-gateway.png",
            "console-memory.png",
            "console-observability.png",
            "sample-output.png",
        ]

    def test_all_entries_are_tuples_of_two_strings(self) -> None:
        for entry in CONSOLE_SCREENSHOTS:
            assert isinstance(entry, tuple)
            assert len(entry) == 2
            assert isinstance(entry[0], str)
            assert isinstance(entry[1], str)


# ---------------------------------------------------------------------------
# capture_console_screenshots
# ---------------------------------------------------------------------------


class TestCaptureConsoleScreenshots:
    def test_returns_five_results_on_success(self, tmp_path: object) -> None:
        browser = FakeBrowser()
        collector = ErrorCollector()
        results = capture_console_screenshots(
            browser, str(tmp_path), collector
        )
        assert len(results) == 5
        assert all(r.success for r in results)
        assert not collector.has_errors

    def test_continues_after_failure(self, tmp_path: object) -> None:
        """When one capture fails, the rest still succeed."""
        # Fail on the second console target (Gateway description)
        fail_url = CONSOLE_SCREENSHOTS[1][1]
        browser = FakeBrowser(fail_urls={fail_url})
        collector = ErrorCollector()

        results = capture_console_screenshots(
            browser, str(tmp_path), collector
        )

        assert len(results) == 5
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        assert len(successes) == 4
        assert len(failures) == 1
        assert failures[0].filename == "console-gateway.png"
        assert failures[0].error is not None
        assert collector.has_errors
        assert len(collector.errors) == 1

    def test_viewport_defaults(self, tmp_path: object) -> None:
        browser = FakeBrowser()
        collector = ErrorCollector()
        results = capture_console_screenshots(
            browser, str(tmp_path), collector
        )
        for r in results:
            assert r.viewport_width == 1440
            assert r.viewport_height == 900
            assert r.wait_seconds == 3

    def test_browser_called_with_correct_params(self, tmp_path: object) -> None:
        browser = FakeBrowser()
        collector = ErrorCollector()
        capture_console_screenshots(browser, str(tmp_path), collector)

        assert len(browser.calls) == 5
        for call in browser.calls:
            assert call["viewport_width"] == 1440
            assert call["viewport_height"] == 900
            assert call["wait_seconds"] == 3


# ---------------------------------------------------------------------------
# Research screenshot filename generation
# ---------------------------------------------------------------------------


class TestSourceSlug:
    def test_simple_domain(self) -> None:
        assert _source_slug("example.com") == "example-com"

    def test_path_with_slashes(self) -> None:
        assert _source_slug("kiro.dev/changelog/ide/") == "kiro-dev-changelog-ide"

    def test_uppercase_normalised(self) -> None:
        assert _source_slug("AWS.Amazon.COM") == "aws-amazon-com"

    def test_special_characters_stripped(self) -> None:
        assert _source_slug("github.com/awslabs/") == "github-com-awslabs"

    def test_empty_string_returns_unknown(self) -> None:
        assert _source_slug("") == "unknown"


# ---------------------------------------------------------------------------
# capture_research_screenshots
# ---------------------------------------------------------------------------


class TestCaptureResearchScreenshots:
    def test_generates_correct_filenames(self, tmp_path: object) -> None:
        articles = [
            _make_article(
                url="https://kiro.dev/changelog",
                source="kiro.dev/changelog/ide/",
            ),
            _make_article(
                url="https://aws.amazon.com/new/article1",
                source="aws.amazon.com/new/",
            ),
        ]
        browser = FakeBrowser()
        collector = ErrorCollector()
        results = capture_research_screenshots(
            browser, articles, str(tmp_path), date(2025, 7, 14), collector
        )

        assert len(results) == 2
        assert results[0].filename == "research/2025-07-14-kiro-dev-changelog-ide.png"
        assert results[1].filename == "research/2025-07-14-aws-amazon-com-new.png"

    def test_skips_non_confirmed_articles(self, tmp_path: object) -> None:
        articles = [
            _make_article(status="confirmed"),
            _make_article(status="discovered"),
            _make_article(status="excluded"),
        ]
        browser = FakeBrowser()
        collector = ErrorCollector()
        results = capture_research_screenshots(
            browser, articles, str(tmp_path), date(2025, 7, 14), collector
        )
        assert len(results) == 1

    def test_continues_after_failure(self, tmp_path: object) -> None:
        articles = [
            _make_article(url="https://fail.example.com", source="fail.com"),
            _make_article(url="https://ok.example.com", source="ok.com"),
        ]
        browser = FakeBrowser(fail_urls={"https://fail.example.com"})
        collector = ErrorCollector()

        results = capture_research_screenshots(
            browser, articles, str(tmp_path), date(2025, 7, 14), collector
        )

        assert len(results) == 2
        assert not results[0].success
        assert results[0].error is not None
        assert results[1].success
        assert collector.has_errors
        assert len(collector.errors) == 1

    def test_empty_article_list(self, tmp_path: object) -> None:
        browser = FakeBrowser()
        collector = ErrorCollector()
        results = capture_research_screenshots(
            browser, [], str(tmp_path), date(2025, 7, 14), collector
        )
        assert results == []


# ---------------------------------------------------------------------------
# capture_all_screenshots
# ---------------------------------------------------------------------------


class TestCaptureAllScreenshots:
    def test_combines_console_and_research(self, tmp_path: object) -> None:
        articles = [
            _make_article(source="kiro.dev/blog/"),
        ]
        browser = FakeBrowser()
        collector = ErrorCollector()

        results = capture_all_screenshots(
            browser, articles, str(tmp_path), date(2025, 7, 14), collector
        )

        # 5 console + 1 research
        assert len(results) == 6
        console_results = results[:5]
        research_results = results[5:]
        assert all(r.success for r in console_results)
        assert len(research_results) == 1
        assert research_results[0].filename.startswith("research/")

    def test_no_articles_returns_only_console(self, tmp_path: object) -> None:
        browser = FakeBrowser()
        collector = ErrorCollector()
        results = capture_all_screenshots(
            browser, [], str(tmp_path), date(2025, 7, 14), collector
        )
        assert len(results) == 5


# ---------------------------------------------------------------------------
# ScreenshotCapture defaults
# ---------------------------------------------------------------------------


class TestScreenshotCaptureDefaults:
    def test_viewport_defaults(self) -> None:
        cap = ScreenshotCapture(target_url="https://example.com", filename="test.png")
        assert cap.viewport_width == 1440
        assert cap.viewport_height == 900
        assert cap.wait_seconds == 3
        assert cap.success is False
        assert cap.error is None


# ---------------------------------------------------------------------------
# Failure logging via ErrorCollector
# ---------------------------------------------------------------------------


class TestFailureLogging:
    def test_console_failure_logged_with_target_and_filename(
        self, tmp_path: object
    ) -> None:
        fail_url = CONSOLE_SCREENSHOTS[0][1]  # Runtime description
        browser = FakeBrowser(fail_urls={fail_url})
        collector = ErrorCollector()

        capture_console_screenshots(browser, str(tmp_path), collector)

        assert len(collector.errors) == 1
        err = collector.errors[0]
        assert err.step == "screenshot"
        assert err.target == fail_url
        assert "filename" in err.context
        assert err.context["filename"] == "console-runtime.png"

    def test_research_failure_logged_with_article_url(
        self, tmp_path: object
    ) -> None:
        articles = [
            _make_article(url="https://broken.example.com", source="broken.com"),
        ]
        browser = FakeBrowser(fail_urls={"https://broken.example.com"})
        collector = ErrorCollector()

        capture_research_screenshots(
            browser, articles, str(tmp_path), date(2025, 7, 14), collector
        )

        assert len(collector.errors) == 1
        err = collector.errors[0]
        assert err.step == "screenshot"
        assert err.target == "https://broken.example.com"
        assert err.context["filename"].startswith("research/")
