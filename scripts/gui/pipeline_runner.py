"""
Bullpen Web GUI — pipeline background thread and approval gate.

This module is responsible for:
- Running ``run_pipeline()`` from ``magic_content_engine.bullpen.editor_in_chief``
  in a background thread so the Flask server stays responsive.
- Providing ``_make_approval_fn``, which replaces the terminal ``input()`` gate
  in ``run_local.py`` with a ``threading.Event`` that the GUI signals via
  ``POST /api/run/approve`` or ``POST /api/run/reject``.

Implemented in Task 3.
"""

from __future__ import annotations

# Standard library
import dataclasses
import json
import logging
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

# Pipeline imports (not modified — read-only consumers)
from magic_content_engine.bullpen.editor_in_chief import run_pipeline
from magic_content_engine.bullpen.models import BullpenBrief, SubeditorReview

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Approval gate
# ---------------------------------------------------------------------------


def _make_approval_fn(run_state: object) -> Callable:
    """Return an approval_fn that blocks until the GUI signals approve/reject.

    The returned callable:
    1. Creates a fresh ``threading.Event`` and stores it on ``run_state``.
    2. Blocks on ``run_state.approval_event.wait()``.
    3. Returns ``run_state.approval_result``.

    The Flask endpoints ``POST /api/run/approve`` and ``POST /api/run/reject``
    set ``approval_result`` and call ``approval_event.set()`` to unblock.
    """

    def approval_fn(sub_review: SubeditorReview | None) -> bool:
        # Create a new event for this gate
        event = threading.Event()
        run_state.approval_event = event  # type: ignore[attr-defined]
        run_state.approval_result = False  # type: ignore[attr-defined]

        logger.info("Approval gate waiting — blocking pipeline thread")
        event.wait()

        result: bool = run_state.approval_result  # type: ignore[attr-defined]
        logger.info("Approval gate unblocked — result=%s", result)
        return result

    return approval_fn


# ---------------------------------------------------------------------------
# Pipeline thread
# ---------------------------------------------------------------------------


def _pipeline_thread(run_state: object, brief: BullpenBrief) -> None:
    """Run ``run_pipeline()`` in a background thread.

    On completion: sets ``run_state.in_progress = False``.
    On unhandled exception: logs full traceback, sets ``run_state.in_progress = False``,
    and writes a synthetic ``pipeline_complete`` event with ``status=error`` to
    the log file at ``run_state.log_path``.

    Parameters
    ----------
    run_state:
        A RunState-like object with fields: ``in_progress``, ``log_path``,
        ``approval_event``, ``approval_result``.
    brief:
        The BullpenBrief for this pipeline run.
    """
    try:
        # Build agent callables — import here to avoid circular imports at
        # module load time and to keep the dependency on AWS services lazy.
        from scripts.gui._agent_factory import build_agent_callables

        agent_callables = build_agent_callables(run_state, brief)

        approval_fn = _make_approval_fn(run_state)

        run_pipeline(
            brief=brief,
            approval_fn=approval_fn,
            **agent_callables,
        )

    except Exception:
        tb = traceback.format_exc()
        logger.error("Unhandled exception in pipeline thread:\n%s", tb)

        # Write synthetic pipeline_complete error event to the log file
        _write_error_event(run_state, tb)

    finally:
        run_state.in_progress = False  # type: ignore[attr-defined]
        logger.info("Pipeline thread finished — in_progress cleared")


def _write_error_event(run_state: object, tb: str) -> None:
    """Append a synthetic pipeline_complete error event to the run log file."""
    log_path: Path | None = getattr(run_state, "log_path", None)
    if log_path is None:
        logger.warning("No log_path on run_state — cannot write error event")
        return

    event = {
        "event_type": "pipeline_complete",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent_type": "editor_in_chief",
        "run_id": getattr(run_state, "run_id", ""),
        "details": {
            "status": "error",
            "error": tb.splitlines()[-1] if tb else "Unknown error",
            "traceback": tb,
        },
    }
    try:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as write_exc:
        logger.error("Failed to write error event to log: %s", write_exc)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_pipeline_thread(run_state: object, brief: BullpenBrief) -> threading.Thread:
    """Create and start a background thread that runs the pipeline.

    Parameters
    ----------
    run_state:
        A RunState-like object (see ``_pipeline_thread`` for required fields).
    brief:
        The BullpenBrief for this pipeline run.

    Returns
    -------
    threading.Thread
        The started thread (daemon=True so it does not block process exit).
    """
    thread = threading.Thread(
        target=_pipeline_thread,
        args=(run_state, brief),
        daemon=True,
        name=f"pipeline-{getattr(run_state, 'run_id', 'unknown')}",
    )
    thread.start()
    return thread
