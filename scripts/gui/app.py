"""
Bullpen Web GUI — Flask application.

All API endpoints and the Flask app instance live here.
Blueprints will be registered as each feature area is implemented.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify

app = Flask(__name__, static_folder="static", static_url_path="/static")


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------


@dataclass
class RunState:
    """Mutable state for a single pipeline run.

    Protected by ``_run_lock`` for reads/writes that must be atomic.
    """

    in_progress: bool = False
    run_id: str = ""
    approval_event: Optional[threading.Event] = None
    approval_result: bool = False
    log_path: Optional[Path] = None
    output_dir: str = ""


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
# Static entry point
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Serve the single-page application shell."""
    return app.send_static_file("index.html")


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
