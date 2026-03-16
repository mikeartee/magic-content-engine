"""Error handling foundation for the Magic Content Engine.

Implements:
- StepError dataclass for structured error collection
- Log-and-continue error collection pattern (ErrorCollector)
- S3 retry with exponential backoff (1s, 2s, 4s)
- Source crawl retry with fixed delay (3 attempts, 2s)
- SES failure logging without retry

Requirements: REQ-027.1–REQ-027.5, REQ-024.2
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from magic_content_engine.config import MAX_RETRY_ATTEMPTS

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class StepError:
    """A single error captured during a workflow step.

    Attributes:
        step: Workflow step name, e.g. "crawl", "score", "extract",
              "generate", "screenshot", "upload".
        target: The item that failed, e.g. source URL, article URL,
                output type, screenshot filename.
        error_message: Human-readable description of the failure.
        context: Additional context such as retry count, model used, etc.
    """

    step: str
    target: str
    error_message: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for agent-log.json."""
        return {
            "step": self.step,
            "target": self.target,
            "message": self.error_message,
            "context": self.context,
        }


class ErrorCollector:
    """Collects StepErrors during a run without aborting.

    Implements the log-and-continue pattern: each error is logged at
    ERROR level and appended to an internal list.  At the end of the
    run the full list is written to agent-log.json and the terminal
    summary.
    """

    def __init__(self) -> None:
        self._errors: list[StepError] = []

    @property
    def errors(self) -> list[StepError]:
        """Return a shallow copy of collected errors."""
        return list(self._errors)

    @property
    def has_errors(self) -> bool:
        return len(self._errors) > 0

    def add(self, error: StepError) -> None:
        """Record an error and log it."""
        self._errors.append(error)
        logger.error(
            "StepError [%s] %s: %s  context=%s",
            error.step,
            error.target,
            error.error_message,
            error.context,
        )

    def to_list(self) -> list[dict[str, Any]]:
        """Serialise all errors for agent-log.json."""
        return [e.to_dict() for e in self._errors]

    def clear(self) -> None:
        """Reset the collector (useful in tests)."""
        self._errors.clear()


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

_S3_BACKOFF_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
_CRAWL_FIXED_DELAY: float = 2.0


def retry_s3(
    fn: Callable[..., T],
    *args: Any,
    max_attempts: int = MAX_RETRY_ATTEMPTS,
    collector: ErrorCollector | None = None,
    target: str = "",
    **kwargs: Any,
) -> T:
    """Call *fn* with S3 exponential-backoff retry.

    Delays between attempts: 1s, 2s, 4s (capped to available slots).
    On final failure the error is added to *collector* and re-raised.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts:
                delay = _S3_BACKOFF_DELAYS[attempt - 1] if attempt - 1 < len(_S3_BACKOFF_DELAYS) else _S3_BACKOFF_DELAYS[-1]
                logger.warning(
                    "S3 retry %d/%d for %s — waiting %.1fs: %s",
                    attempt,
                    max_attempts,
                    target,
                    delay,
                    exc,
                )
                time.sleep(delay)

    # All attempts exhausted
    assert last_exc is not None
    if collector is not None:
        collector.add(
            StepError(
                step="upload",
                target=target,
                error_message=str(last_exc),
                context={"retry_count": max_attempts},
            )
        )
    raise last_exc


def retry_crawl(
    fn: Callable[..., T],
    *args: Any,
    max_attempts: int = MAX_RETRY_ATTEMPTS,
    collector: ErrorCollector | None = None,
    target: str = "",
    **kwargs: Any,
) -> T | None:
    """Call *fn* with fixed-delay retry for source crawling.

    3 attempts with a 2-second fixed delay between each.
    On final failure the error is added to *collector* and ``None``
    is returned (log-and-continue).
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts:
                logger.warning(
                    "Crawl retry %d/%d for %s — waiting %.1fs: %s",
                    attempt,
                    max_attempts,
                    target,
                    _CRAWL_FIXED_DELAY,
                    exc,
                )
                time.sleep(_CRAWL_FIXED_DELAY)

    # All attempts exhausted — log and continue
    assert last_exc is not None
    if collector is not None:
        collector.add(
            StepError(
                step="crawl",
                target=target,
                error_message=str(last_exc),
                context={"retry_count": max_attempts},
            )
        )
    logger.error("Source crawl failed after %d attempts: %s", max_attempts, target)
    return None


def log_ses_failure(
    error: Exception,
    target: str,
    collector: ErrorCollector | None = None,
) -> None:
    """Log an SES delivery failure without retrying.

    SES failures are recorded but never retried per the design spec.
    """
    step_error = StepError(
        step="ses",
        target=target,
        error_message=str(error),
        context={"retry": False},
    )
    if collector is not None:
        collector.add(step_error)
    else:
        logger.error(
            "SES failure (no collector) [%s] %s: %s",
            step_error.step,
            step_error.target,
            step_error.error_message,
        )
