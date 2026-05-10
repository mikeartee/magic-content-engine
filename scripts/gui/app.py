"""
Bullpen Web GUI — Flask application.

All API endpoints and the Flask app instance live here.
Blueprints will be registered as each feature area is implemented.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional

from flask import Flask, jsonify, request

# Pipeline imports (read-only consumers — pipeline package is not modified)
from magic_content_engine.bullpen.models import BullpenBrief

# pipeline_runner is implemented in Task 3; imported here so the background
# thread call is wired up correctly even though the module is a stub for now.
# Try relative import first (when loaded as part of the scripts.gui package),
# fall back to direct import (when scripts/gui is on sys.path directly).
try:
    from . import pipeline_runner
except ImportError:
    import pipeline_runner  # type: ignore[no-redef]

app = Flask(__name__, static_folder="static", static_url_path="/static")


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------


@dataclass
class RunState:
    """Mutable state for the currently active (or most recent) pipeline run."""

    in_progress: bool = False
    run_id: Optional[str] = None
    approval_event: Optional[threading.Event] = None
    approval_result: Optional[bool] = None
    log_path: Optional[str] = None
    output_dir: Optional[str] = None


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
        outputs (list[str]): At least one output type from
            {blog, youtube, cfp, usergroup, digest}.
        dry_run (bool, optional): Passed through to the pipeline (default False).

    Returns:
        202 {"run_id": "<hex>"} on success.
        409 if a run is already in progress.
        422 if validation fails.
    """
    body = request.get_json(silent=True) or {}

    topic: str = (body.get("topic") or "").strip()
    outputs: list = body.get("outputs") or []

    # Validate topic
    if not topic:
        return (
            jsonify({"error": "validation", "detail": "topic must be non-empty"}),
            422,
        )

    # Validate outputs
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
        _run_state.approval_event = threading.Event()
        _run_state.approval_result = None
        _run_state.log_path = None
        _run_state.output_dir = None

    # Start the background pipeline thread.
    # pipeline_runner.run_pipeline_thread is implemented in Task 3.
    t = threading.Thread(
        target=pipeline_runner.run_pipeline_thread,
        args=(_run_state, brief),
        daemon=True,
    )
    t.start()

    return jsonify({"run_id": run_id}), 202


# ---------------------------------------------------------------------------
# Static entry point
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Serve the single-page application shell."""
    return app.send_static_file("index.html")
