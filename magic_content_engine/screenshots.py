"""Screenshot capture service using AgentCore Browser.

Captures console screenshots and research article screenshots at
1440×900 viewport with a 3-second wait after navigation.

Requirements: REQ-016.1–REQ-016.5
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Protocol

from magic_content_engine.errors import ErrorCollector, StepError
from magic_content_engine.models import Article, ScreenshotCapture

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Browser protocol — testable seam
# ---------------------------------------------------------------------------


class BrowserProtocol(Protocol):
    """Protocol for browser screenshot calls.

    Any callable matching this signature can be injected, making the
    screenshot service testable without a real AgentCore Browser.
    """

    def capture(
        self,
        url: str,
        viewport_width: int,
        viewport_height: int,
        wait_seconds: int,
    ) -> bytes:
        """Navigate to *url*, wait, and return a PNG screenshot as bytes.

        Raises:
            Exception: On any capture failure.
        """
        ...


# ---------------------------------------------------------------------------
# Console screenshot targets
# ---------------------------------------------------------------------------

CONSOLE_SCREENSHOTS: list[tuple[str, str]] = [
    ("console-runtime.png", "AgentCore Runtime dashboard"),
    ("console-gateway.png", "AgentCore Gateway tool list"),
    ("console-memory.png", "AgentCore Memory records"),
    ("console-observability.png", "AgentCore Observability trace"),
    ("sample-output.png", "Generated digest rendered as HTML"),
]


# ---------------------------------------------------------------------------
# Slug helper for research screenshot filenames
# ---------------------------------------------------------------------------

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _source_slug(source: str) -> str:
    """Derive a kebab-case slug from an article source string."""
    slug = _NON_ALNUM_RE.sub("-", source.lower()).strip("-")
    # Collapse consecutive hyphens
    slug = re.sub(r"-{2,}", "-", slug)
    return slug or "unknown"


# ---------------------------------------------------------------------------
# Capture functions
# ---------------------------------------------------------------------------


def capture_console_screenshots(
    browser: BrowserProtocol,
    screenshots_path: str,
    collector: ErrorCollector,
) -> list[ScreenshotCapture]:
    """Capture the five console screenshots.

    Iterates through ``CONSOLE_SCREENSHOTS``.  On failure for any single
    capture the error is logged via *collector* and the function continues
    to the next target.
    """
    results: list[ScreenshotCapture] = []

    for filename, description in CONSOLE_SCREENSHOTS:
        # Use the description as a placeholder URL (real URLs come from config)
        target_url = description
        cap = ScreenshotCapture(target_url=target_url, filename=filename)

        try:
            data = browser.capture(
                url=target_url,
                viewport_width=cap.viewport_width,
                viewport_height=cap.viewport_height,
                wait_seconds=cap.wait_seconds,
            )
            # Write the PNG to disk
            dest = os.path.join(screenshots_path, filename)
            os.makedirs(os.path.dirname(dest) or screenshots_path, exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)
            cap.success = True
        except Exception as exc:
            cap.success = False
            cap.error = str(exc)
            collector.add(
                StepError(
                    step="screenshot",
                    target=target_url,
                    error_message=str(exc),
                    context={"filename": filename},
                )
            )

        results.append(cap)

    return results


def capture_research_screenshots(
    browser: BrowserProtocol,
    articles: list[Article],
    screenshots_path: str,
    run_date: date,
    collector: ErrorCollector,
) -> list[ScreenshotCapture]:
    """Capture a screenshot for each confirmed article.

    Filename pattern: ``research/YYYY-MM-DD-[source-slug].png``
    Only articles with status ``"confirmed"`` are captured.
    On failure the error is logged and the function continues.
    """
    results: list[ScreenshotCapture] = []
    date_prefix = run_date.isoformat()

    for article in articles:
        if article.status != "confirmed":
            continue

        slug = _source_slug(article.source)
        filename = f"research/{date_prefix}-{slug}.png"
        target_url = article.url
        cap = ScreenshotCapture(target_url=target_url, filename=filename)

        try:
            data = browser.capture(
                url=target_url,
                viewport_width=cap.viewport_width,
                viewport_height=cap.viewport_height,
                wait_seconds=cap.wait_seconds,
            )
            dest = os.path.join(screenshots_path, filename)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)
            cap.success = True
        except Exception as exc:
            cap.success = False
            cap.error = str(exc)
            collector.add(
                StepError(
                    step="screenshot",
                    target=target_url,
                    error_message=str(exc),
                    context={"filename": filename},
                )
            )

        results.append(cap)

    return results


def capture_all_screenshots(
    browser: BrowserProtocol,
    articles: list[Article],
    screenshots_path: str,
    run_date: date,
    collector: ErrorCollector,
) -> list[ScreenshotCapture]:
    """Capture both console and research screenshots.

    Returns the combined list of all ``ScreenshotCapture`` results.
    """
    console = capture_console_screenshots(browser, screenshots_path, collector)
    research = capture_research_screenshots(
        browser, articles, screenshots_path, run_date, collector
    )
    return console + research
