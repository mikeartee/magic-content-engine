"""Tests for AgentCore Observability integration.

Requirements: REQ-023.1, REQ-023.2, REQ-023.3
"""

from __future__ import annotations

import time

import pytest

from magic_content_engine.observability import (
    WORKFLOW_STEPS,
    SpanContext,
    TracingCollector,
    trace_step,
)


# ---------------------------------------------------------------------------
# trace_step — start and end times
# ---------------------------------------------------------------------------


class TestTraceStepTiming:
    def test_records_start_and_end_times(self):
        collector = TracingCollector()
        with trace_step(collector, "score_articles") as span:
            time.sleep(0.01)

        assert span.start_time > 0
        assert span.end_time > 0
        assert span.end_time >= span.start_time

    def test_calculates_duration_ms(self):
        collector = TracingCollector()
        with trace_step(collector, "crawl_sources") as span:
            time.sleep(0.05)

        assert span.duration_ms >= 10  # generous lower bound for OS jitter
        expected = (span.end_time - span.start_time) * 1000
        assert abs(span.duration_ms - expected) < 0.5


# ---------------------------------------------------------------------------
# trace_step — error handling (REQ-023.2)
# ---------------------------------------------------------------------------


class TestTraceStepErrors:
    def test_emits_error_on_exception(self):
        collector = TracingCollector()
        with pytest.raises(RuntimeError, match="boom"):
            with trace_step(collector, "extract_metadata"):
                raise RuntimeError("boom")

        assert len(collector.errors) == 1
        err = collector.errors[0]
        assert err["step_name"] == "extract_metadata"
        assert err["error_message"] == "boom"
        assert err["context"]["exception_type"] == "RuntimeError"

    def test_span_recorded_on_exception(self):
        collector = TracingCollector()
        with pytest.raises(ValueError):
            with trace_step(collector, "build_citations"):
                raise ValueError("bad data")

        assert len(collector.spans) == 1
        assert collector.spans[0].step_name == "build_citations"
        assert collector.spans[0].duration_ms >= 0

    def test_exception_re_raised(self):
        collector = TracingCollector()
        with pytest.raises(TypeError, match="wrong type"):
            with trace_step(collector, "generate_content"):
                raise TypeError("wrong type")


# ---------------------------------------------------------------------------
# TracingCollector — span collection
# ---------------------------------------------------------------------------


class TestTracingCollectorSpans:
    def test_collects_spans(self):
        collector = TracingCollector()
        with trace_step(collector, "crawl_sources"):
            pass
        with trace_step(collector, "score_articles"):
            pass

        assert len(collector.spans) == 2
        assert collector.spans[0].step_name == "crawl_sources"
        assert collector.spans[1].step_name == "score_articles"

    def test_span_has_positive_duration(self):
        collector = TracingCollector()
        with trace_step(collector, "assemble_bundle"):
            time.sleep(0.05)

        assert collector.spans[0].duration_ms > 0


# ---------------------------------------------------------------------------
# TracingCollector — error collection
# ---------------------------------------------------------------------------


class TestTracingCollectorErrors:
    def test_collects_errors(self):
        collector = TracingCollector()
        collector.emit_error("crawl_sources", "timeout", {"url": "https://example.com"})
        collector.emit_error("score_articles", "bad json", {"article": "test"})

        assert len(collector.errors) == 2
        assert collector.errors[0]["step_name"] == "crawl_sources"
        assert collector.errors[1]["error_message"] == "bad json"

    def test_error_from_trace_step_collected(self):
        collector = TracingCollector()
        with pytest.raises(RuntimeError):
            with trace_step(collector, "capture_screenshots"):
                raise RuntimeError("browser crash")

        assert len(collector.errors) == 1
        assert collector.errors[0]["step_name"] == "capture_screenshots"
        assert "browser crash" in collector.errors[0]["error_message"]


# ---------------------------------------------------------------------------
# Workflow step names validation
# ---------------------------------------------------------------------------


class TestWorkflowStepNames:
    """Verify WORKFLOW_STEPS covers the major steps from REQ-023.1."""

    REQUIRED_STEPS = {
        "crawl_sources",
        "score_articles",
        "extract_metadata",
        "build_citations",
        "present_articles",
        "output_choice",
        "generate_content",
        "capture_screenshots",
        "assemble_bundle",
    }

    def test_all_required_steps_present(self):
        for step in self.REQUIRED_STEPS:
            assert step in WORKFLOW_STEPS, f"Missing workflow step: {step}"

    def test_no_empty_step_names(self):
        for step in WORKFLOW_STEPS:
            assert step.strip(), "Empty step name found"
