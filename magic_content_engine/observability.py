"""AgentCore Observability integration for the Magic Content Engine.

Provides trace spans for each major workflow step, per-step latency
recording, and error-level trace events on failures.

Uses a protocol for the observability backend so the module is fully
testable without a live AgentCore connection.

Requirements: REQ-023.1, REQ-023.2, REQ-023.3
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Workflow step names (must match orchestrator._log_step calls)
# ---------------------------------------------------------------------------

WORKFLOW_STEPS: frozenset[str] = frozenset({
    "crawl_sources",
    "score_articles",
    "extract_metadata",
    "build_citations",
    "present_articles",
    "output_choice",
    "generate_content",
    "capture_screenshots",
    "assemble_bundle",
})


# ---------------------------------------------------------------------------
# SpanContext dataclass
# ---------------------------------------------------------------------------


@dataclass
class SpanContext:
    """Represents a single trace span for a workflow step."""

    step_name: str
    start_time: float = 0.0
    end_time: float = 0.0
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# ObservabilityProtocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ObservabilityProtocol(Protocol):
    """Protocol for the observability backend."""

    def start_span(self, step_name: str) -> SpanContext:
        """Begin a trace span for the given step."""
        ...

    def end_span(self, span: SpanContext) -> None:
        """Finalise a trace span after the step completes."""
        ...

    def emit_error(self, step_name: str, error_message: str, context: dict[str, Any]) -> None:
        """Emit an error-level trace event."""
        ...


# ---------------------------------------------------------------------------
# trace_step context manager
# ---------------------------------------------------------------------------


@contextmanager
def trace_step(
    backend: ObservabilityProtocol,
    step_name: str,
) -> Generator[SpanContext, None, None]:
    """Context manager that traces a workflow step.

    On entry: calls ``start_span`` and records the start time.
    On normal exit: records end time, calculates duration, calls ``end_span``.
    On exception: calls ``emit_error`` with step name, error message, and
    context, then re-raises the exception.
    """
    span = backend.start_span(step_name)
    span.start_time = time.monotonic()
    try:
        yield span
    except Exception as exc:
        span.end_time = time.monotonic()
        span.duration_ms = (span.end_time - span.start_time) * 1000
        backend.emit_error(
            step_name=step_name,
            error_message=str(exc),
            context={"exception_type": type(exc).__name__},
        )
        backend.end_span(span)
        raise
    else:
        span.end_time = time.monotonic()
        span.duration_ms = (span.end_time - span.start_time) * 1000
        backend.end_span(span)


# ---------------------------------------------------------------------------
# TracingCollector — testable in-memory backend
# ---------------------------------------------------------------------------


class TracingCollector:
    """In-memory observability backend that collects spans and errors.

    Implements ``ObservabilityProtocol`` and stores all completed spans
    and emitted errors for inspection in tests.
    """

    def __init__(self) -> None:
        self.spans: list[SpanContext] = []
        self.errors: list[dict[str, Any]] = []

    def start_span(self, step_name: str) -> SpanContext:
        return SpanContext(step_name=step_name)

    def end_span(self, span: SpanContext) -> None:
        self.spans.append(span)

    def emit_error(self, step_name: str, error_message: str, context: dict[str, Any]) -> None:
        self.errors.append({
            "step_name": step_name,
            "error_message": error_message,
            "context": context,
        })
