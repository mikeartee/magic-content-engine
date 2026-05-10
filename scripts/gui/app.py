"""
Bullpen Web GUI — Flask application.

All API endpoints and the Flask app instance live here.
Blueprints will be registered as each feature area is implemented.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from flask import Flask, Response, jsonify, request, stream_with_context

# Pipeline imports (read-only consumers — pipeline package is not modified)
from magic_content_engine.bullpen.models import BullpenBrief

# pipeline_runner is implemented in Task 3; imported here so the background
# thread call is wired up correctly.
try:
    from . import pipeline_runner
    from . import log_tailer
except ImportError:
    import pipeline_runner  # type: ignore[no-redef]
    import log_tailer  # type: ignore[no-redef]

app = Flask(__name__, static_folder="static", static_url_path="/static")


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------


@dataclass
class RunState:
    """Mutable state for the currently active (or most recent) pipeline run.

    Protected by ``_run_lock`` for reads/writes that must be atomic.
    """

    in_progress: bool = False
    run_id: Optional[str] = None
    approval_event: Optional[threading.Event] = None
    approval_result: Optional[bool] = None
    log_path: Optional[Path] = None
    output_dir: Optional[str] = None


# Module-level singletons — one run at a time.
_run_state: RunState = RunState()
_run_lock: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.route("/api/health")
def health():
    """Simple liveness probe."""
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# POST /api/run
# ---------------------------------------------------------------------------


@app.route("/api/run", methods=["POST"])
def start_run():
    """Start a new pipeline run.

    Request body (JSON):
        topic (str): Non-empty topic string.
        outputs (list[str]): At least one output type.
        dry_run (bool, optional): Passed through to the pipeline (default False).

    Returns:
        202 {"run_id": "<hex>"} on success.
        409 if a run is already in progress.
        422 if validation fails.
    """
    body = request.get_json(silent=True) or {}

    topic: str = (body.get("topic") or "").strip()
    outputs: list = body.get("outputs") or []

    if not topic:
        return (
            jsonify({"error": "validation", "detail": "topic must be non-empty"}),
            422,
        )

    if not outputs:
        return (
            jsonify(
                {
                    "error": "validation",
                    "detail": "at least one output type must be selected",
                }
            ),
            422,
        )

    with _run_lock:
        if _run_state.in_progress:
            return (
                jsonify(
                    {
                        "error": "conflict",
                        "detail": "A pipeline run is already in progress.",
                    }
                ),
                409,
            )

        run_id = uuid.uuid4().hex[:8]
        brief = BullpenBrief(topic=topic, requested_outputs=list(outputs))

        _run_state.in_progress = True
        _run_state.run_id = run_id
        _run_state.approval_event = None
        _run_state.approval_result = None
        _run_state.log_path = None
        _run_state.output_dir = None

    # Start the background pipeline thread.
    t = threading.Thread(
        target=pipeline_runner.run_pipeline_thread,
        args=(_run_state, brief),
        daemon=True,
    )
    t.start()

    return jsonify({"run_id": run_id}), 202


# ---------------------------------------------------------------------------
# SSE log-tailing endpoint
# ---------------------------------------------------------------------------


@app.route("/api/run/status")
def run_status():
    """Stream pipeline progress as Server-Sent Events.

    Opens ``output/agent-log.jsonl`` (relative to the repo root), seeks to
    the end, and forwards each new JSON line to the browser as an SSE
    ``data:`` frame. A synthetic ``pipeline_complete`` event is emitted
    when the pipeline thread exits.
    """
    generator = log_tailer.tail_log(_run_state)

    response = Response(
        stream_with_context(generator),
        mimetype="text/event-stream",
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


# ---------------------------------------------------------------------------
# Approval gate endpoints
# ---------------------------------------------------------------------------


@app.route("/api/run/approve", methods=["POST"])
def approve_run():
    """Signal the waiting approval gate to approve (return True).

    Returns 200 if the gate was waiting, 409 if no gate is active.
    """
    with _run_lock:
        event = _run_state.approval_event
        if event is None:
            return (
                jsonify({"error": "conflict", "detail": "No approval gate is currently waiting."}),
                409,
            )
        _run_state.approval_result = True
        event.set()

    return jsonify({"status": "approved"}), 200


@app.route("/api/run/reject", methods=["POST"])
def reject_run():
    """Signal the waiting approval gate to reject (return False).

    Returns 200 if the gate was waiting, 409 if no gate is active.
    """
    with _run_lock:
        event = _run_state.approval_event
        if event is None:
            return (
                jsonify({"error": "conflict", "detail": "No approval gate is currently waiting."}),
                409,
            )
        _run_state.approval_result = False
        event.set()

    return jsonify({"status": "rejected"}), 200


# ---------------------------------------------------------------------------
# Static entry point
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Serve the single-page application shell."""
    return app.send_static_file("index.html")
