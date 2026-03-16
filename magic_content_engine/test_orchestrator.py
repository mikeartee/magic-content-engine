"""Tests for the orchestrator workflow.

Covers:
- parse_args with --source manual and --source scheduled
- run_workflow executes all 20 steps in order
- run_workflow records invocation source in AgentLog
- run_workflow continues after step failure (log-and-continue)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pytest

from magic_content_engine.models import (
    Article,
    HeldItem,
    PostEngagement,
    TopicCoverageMap,
)
from magic_content_engine.orchestrator import (
    MemoryProtocol,
    WorkflowDependencies,
    parse_args,
    run_workflow,
)
from magic_content_engine.topic_coverage import create_empty_coverage_map


# ---------------------------------------------------------------------------
# Stub implementations for all protocols
# ---------------------------------------------------------------------------


class StubMemory:
    """Stub for MemoryProtocol."""

    def __init__(self, voice: str = "test voice", urls: set[str] | None = None):
        self.voice = voice
        self.urls = urls or set()
        self.stored_urls: set[str] = set()

    def load_voice_profile(self) -> str:
        return self.voice

    def load_covered_urls(self) -> set[str]:
        return self.urls

    def store_covered_urls(self, urls: set[str], run_date: date) -> None:
        self.stored_urls = urls


class StubDedupMemory:
    """Stub for deduplication.MemoryProtocol."""

    def __init__(self, known_urls: set[str] | None = None):
        self.known = known_urls or set()
        self.stored: list[tuple[str, date]] = []

    def is_url_previously_covered(self, url: str) -> bool:
        return url in self.known

    def store_article_url(self, url: str, run_date: date) -> None:
        self.stored.append((url, run_date))


class StubTopicMemory:
    """Stub for TopicCoverageMemoryProtocol."""

    def __init__(self, coverage_map: TopicCoverageMap | None = None):
        self._map = coverage_map
        self.saved: TopicCoverageMap | None = None

    def load_topic_coverage_map(self) -> TopicCoverageMap | None:
        return self._map

    def save_topic_coverage_map(self, coverage_map: TopicCoverageMap) -> None:
        self.saved = coverage_map


class StubDevToAPI:
    """Stub for DevToAPIProtocol."""

    def __init__(self, articles: list[dict] | None = None):
        self._articles = articles or []

    def fetch_user_articles(self, username: str, api_key: str) -> list[dict]:
        return self._articles


class StubEngagementMemory:
    """Stub for EngagementMemoryProtocol."""

    def __init__(self) -> None:
        self._engagements: list[PostEngagement] = []

    def load_engagements(self) -> list[PostEngagement]:
        return self._engagements

    def save_engagements(self, engagements: list[PostEngagement]) -> None:
        self._engagements = engagements


class StubHeldItemMemory:
    """Stub for HeldItemMemoryProtocol."""

    def __init__(self, items: list[HeldItem] | None = None):
        self._items = items or []

    def load_held_items(self) -> list[HeldItem]:
        return self._items

    def save_held_item(self, item: HeldItem) -> None:
        self._items.append(item)

    def remove_held_item(self, item: HeldItem) -> None:
        self._items = [i for i in self._items if i.filename != item.filename]


class StubSES:
    """Stub for SESNotifierProtocol."""

    def __init__(self) -> None:
        self.sent: list[HeldItem] = []

    def send_embargo_release(self, item: HeldItem) -> None:
        self.sent.append(item)


class StubBrowser:
    """Stub for BrowserProtocol (crawler)."""

    def __init__(self) -> None:
        self.fetched_urls: list[str] = []

    def fetch_page(self, url: str):
        self.fetched_urls.append(url)
        from magic_content_engine.crawler import CrawlResult
        return CrawlResult(url=url, content="test content", title="Test Article")


class StubScreenshotBrowser:
    """Stub for screenshots.BrowserProtocol."""

    def __init__(self) -> None:
        self.captured: list[str] = []

    def capture(self, url: str, viewport_width: int, viewport_height: int, wait_seconds: int) -> bytes:
        self.captured.append(url)
        return b"\x89PNG"


class StubLLM:
    """Stub LLM that returns valid JSON for scoring/metadata/citation."""

    def __call__(self, *args, **kwargs) -> str:
        return '{"score": 4, "rationale": "Relevant"}'


class StubLLMWriter:
    """Stub LLM for writing agent (keyword args)."""

    def __call__(self, *, model_id: str, prompt: str) -> str:
        return "Generated content for testing."


class StubS3Client:
    """Stub for S3ClientProtocol."""

    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str, str]] = []

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        self.uploaded.append((local_path, bucket, key))


class StubBundleFileOps:
    """Stub for bundle.FileOps."""

    def __init__(self) -> None:
        self.written: list[str] = []
        self.dirs: list[str] = []

    def write_text(self, path: str, content: str) -> None:
        self.written.append(path)

    def write_json(self, path: str, data: dict) -> None:
        self.written.append(path)

    def ensure_dir(self, path: str) -> None:
        self.dirs.append(path)


class StubGateFileOps:
    """Stub for publish_gate.FileOps."""

    def move_file(self, src: str, dest_dir: str, filename: str) -> str:
        return f"{dest_dir}/{filename}"


# ---------------------------------------------------------------------------
# Call tracker for step ordering
# ---------------------------------------------------------------------------


class CallTracker:
    """Tracks the order of input_fn calls to verify step sequencing."""

    def __init__(self, responses: list[str] | None = None):
        self.calls: list[str] = []
        self._responses = list(responses or [])
        self._idx = 0

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return ""


# ---------------------------------------------------------------------------
# Fixture: build default deps
# ---------------------------------------------------------------------------


def _make_deps(**overrides) -> WorkflowDependencies:
    """Build a WorkflowDependencies with all stubs, applying overrides."""
    defaults = dict(
        memory=StubMemory(),
        dedup_memory=StubDedupMemory(),
        topic_memory=StubTopicMemory(),
        engagement_api=StubDevToAPI(),
        engagement_memory=StubEngagementMemory(),
        held_item_memory=StubHeldItemMemory(),
        ses_notifier=StubSES(),
        browser=StubBrowser(),
        llm_scorer=StubLLM(),
        llm_extractor=StubLLM(),
        llm_formatter=StubLLM(),
        llm_writer=StubLLMWriter(),
        screenshot_browser=StubScreenshotBrowser(),
        s3_client=StubS3Client(),
        bundle_file_ops=StubBundleFileOps(),
        gate_file_ops=StubGateFileOps(),
        input_fn=CallTracker(["", "1,2"]),  # confirm articles, select blog+youtube
        unattended=True,  # skip interactive prompts
    )
    defaults.update(overrides)
    return WorkflowDependencies(**defaults)


# ---------------------------------------------------------------------------
# Tests: parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_default_source_is_manual(self):
        args = parse_args([])
        assert args.source == "manual"

    def test_source_manual(self):
        args = parse_args(["--source", "manual"])
        assert args.source == "manual"

    def test_source_scheduled(self):
        args = parse_args(["--source", "scheduled"])
        assert args.source == "scheduled"

    def test_run_date_override(self):
        args = parse_args(["--run-date", "2025-07-14"])
        assert args.run_date == "2025-07-14"

    def test_run_date_default_none(self):
        args = parse_args([])
        assert args.run_date is None


# ---------------------------------------------------------------------------
# Tests: run_workflow
# ---------------------------------------------------------------------------


class TestRunWorkflow:
    def test_executes_all_steps_in_order(self):
        """Verify all 20 workflow steps execute in the correct order."""
        deps = _make_deps()
        log = run_workflow(deps, source="manual", run_date=date(2025, 7, 14))

        expected_steps = [
            "accept_trigger",
            "load_memory",
            "fetch_engagement",
            "load_topic_coverage",
            "weekly_brief",
            "check_embargo",
            "crawl_sources",
            "deduplicate",
            "score_articles",
            "extract_metadata",
            "build_citations",
            "present_articles",
            "output_choice",
            "generate_content",
            "update_topic_coverage",
            "capture_screenshots",
            "assemble_bundle",
            "publish_gate",
            "s3_upload",
            "store_urls",
            "terminal_summary",
        ]
        actual_steps = log.run_metadata.get("steps", [])
        assert actual_steps == expected_steps

    def test_records_invocation_source_manual(self):
        deps = _make_deps()
        log = run_workflow(deps, source="manual", run_date=date(2025, 7, 14))
        assert log.invocation_source == "manual"

    def test_records_invocation_source_scheduled(self):
        deps = _make_deps()
        log = run_workflow(deps, source="scheduled", run_date=date(2025, 7, 14))
        assert log.invocation_source == "scheduled"

    def test_records_run_date(self):
        deps = _make_deps()
        log = run_workflow(deps, source="manual", run_date=date(2025, 7, 14))
        assert log.run_date == "2025-07-14"

    def test_continues_after_memory_failure(self):
        """Workflow should continue even if memory loading fails."""

        class FailingMemory:
            def load_voice_profile(self) -> str:
                raise RuntimeError("Memory unavailable")

            def load_covered_urls(self) -> set[str]:
                raise RuntimeError("Memory unavailable")

            def store_covered_urls(self, urls, run_date):
                pass

        deps = _make_deps(memory=FailingMemory())
        log = run_workflow(deps, source="manual", run_date=date(2025, 7, 14))

        # Should still complete all steps
        assert "terminal_summary" in log.run_metadata["steps"]
        # Should have recorded the error
        error_steps = [e["step"] for e in log.errors]
        assert "load_memory" in error_steps

    def test_continues_after_crawl_failure(self):
        """Workflow should continue even if browser crawling raises."""

        class FailingBrowser:
            def fetch_page(self, url: str):
                raise ConnectionError("Network down")

        deps = _make_deps(browser=FailingBrowser())
        log = run_workflow(deps, source="manual", run_date=date(2025, 7, 14))

        # All steps should still be recorded
        assert "terminal_summary" in log.run_metadata["steps"]

    def test_selected_outputs_recorded(self):
        deps = _make_deps(unattended=True)
        log = run_workflow(deps, source="manual", run_date=date(2025, 7, 14))
        # Unattended defaults to blog + youtube
        assert "blog" in log.selected_outputs
        assert "youtube" in log.selected_outputs


# ---------------------------------------------------------------------------
# Tests: _print_terminal_summary
# ---------------------------------------------------------------------------


from magic_content_engine.orchestrator import _print_terminal_summary
from magic_content_engine.errors import ErrorCollector, StepError
from magic_content_engine.models import Article, CostEstimate


def _make_articles(n: int) -> list[Article]:
    """Create n stub articles for summary testing."""
    return [
        Article(
            url=f"https://example.com/article-{i}",
            title=f"Article {i}",
            source="example.com",
            source_type="primary",
            discovered_date=date(2025, 7, 14),
        )
        for i in range(n)
    ]


def _make_cost(total: float = 0.0) -> CostEstimate:
    return CostEstimate(
        invocations=[],
        total_llm_cost_usd=0.0,
        total_agentcore_cost_usd=0.0,
        total_cost_usd=total,
    )


class TestTerminalSummary:
    """Tests for _print_terminal_summary output.

    Validates: REQ-025.1, REQ-025.2, REQ-027.5
    """

    def test_articles_found_count(self, capsys):
        _print_terminal_summary(
            all_articles=_make_articles(7),
            confirmed_articles=_make_articles(3),
            selected_outputs=["blog"],
            cost_estimate=_make_cost(),
            screenshot_results=[],
            collector=ErrorCollector(),
            uploaded_keys=[],
        )
        out = capsys.readouterr().out
        assert "Articles found:    7" in out

    def test_articles_kept_count(self, capsys):
        _print_terminal_summary(
            all_articles=_make_articles(10),
            confirmed_articles=_make_articles(4),
            selected_outputs=["blog"],
            cost_estimate=_make_cost(),
            screenshot_results=[],
            collector=ErrorCollector(),
            uploaded_keys=[],
        )
        out = capsys.readouterr().out
        assert "Articles kept:     4" in out

    def test_outputs_generated(self, capsys):
        _print_terminal_summary(
            all_articles=_make_articles(2),
            confirmed_articles=_make_articles(2),
            selected_outputs=["blog", "youtube"],
            cost_estimate=_make_cost(),
            screenshot_results=[],
            collector=ErrorCollector(),
            uploaded_keys=[],
        )
        out = capsys.readouterr().out
        assert "blog" in out
        assert "youtube" in out

    def test_estimated_cost(self, capsys):
        _print_terminal_summary(
            all_articles=_make_articles(1),
            confirmed_articles=_make_articles(1),
            selected_outputs=["blog"],
            cost_estimate=_make_cost(total=0.004512),
            screenshot_results=[],
            collector=ErrorCollector(),
            uploaded_keys=[],
        )
        out = capsys.readouterr().out
        assert "$0.004512" in out

    def test_clean_run_when_no_errors(self, capsys):
        _print_terminal_summary(
            all_articles=_make_articles(3),
            confirmed_articles=_make_articles(2),
            selected_outputs=["blog"],
            cost_estimate=_make_cost(),
            screenshot_results=[],
            collector=ErrorCollector(),
            uploaded_keys=[],
        )
        out = capsys.readouterr().out
        assert "Clean run" in out

    def test_errors_included_when_present(self, capsys):
        collector = ErrorCollector()
        collector.add(StepError(step="crawl", target="example.com", error_message="Connection refused"))
        collector.add(StepError(step="score", target="article-1", error_message="LLM timeout"))

        _print_terminal_summary(
            all_articles=_make_articles(5),
            confirmed_articles=_make_articles(3),
            selected_outputs=["blog"],
            cost_estimate=_make_cost(),
            screenshot_results=[],
            collector=collector,
            uploaded_keys=[],
        )
        out = capsys.readouterr().out
        assert "Errors (2)" in out
        assert "Connection refused" in out
        assert "LLM timeout" in out
        assert "Clean run" not in out

    def test_failed_screenshots_shown(self, capsys):
        results = [
            {"filename": "console-runtime.png", "success": True, "error": None},
            {"filename": "console-gateway.png", "success": False, "error": "Navigation timeout"},
        ]
        _print_terminal_summary(
            all_articles=_make_articles(2),
            confirmed_articles=_make_articles(2),
            selected_outputs=["blog"],
            cost_estimate=_make_cost(),
            screenshot_results=results,
            collector=ErrorCollector(),
            uploaded_keys=[],
        )
        out = capsys.readouterr().out
        assert "Failed screenshots: 1" in out
        assert "console-gateway.png" in out
        assert "Navigation timeout" in out

    def test_no_failed_screenshots_section_when_all_succeed(self, capsys):
        results = [
            {"filename": "console-runtime.png", "success": True, "error": None},
            {"filename": "console-gateway.png", "success": True, "error": None},
        ]
        _print_terminal_summary(
            all_articles=_make_articles(2),
            confirmed_articles=_make_articles(2),
            selected_outputs=["blog"],
            cost_estimate=_make_cost(),
            screenshot_results=results,
            collector=ErrorCollector(),
            uploaded_keys=[],
        )
        out = capsys.readouterr().out
        assert "Failed screenshots" not in out
