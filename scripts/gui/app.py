"""
Bullpen Web GUI — Flask application.

All API endpoints and the Flask app instance live here.
Blueprints will be registered as each feature area is implemented.
"""

# Standard library
import threading
from dataclasses import dataclass, field
from pathlib import Path

# Third-party
from flask import Flask, Response, jsonify, stream_with_context

# Local
from scripts.gui import log_tailer

app = Flask(__name__, static_folder="static", static_url_path="/static")


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------


@dataclass
class RunState:
    """Mutable state for a single pipeline run.

    Protected by ``_run_lock`` for all reads/writes outside the pipeline
    thread itself.
    """

    in_progress: bool = False
    run_id: str = ""
    approval_event: threading.Event = field(default_factory=threading.Event)
    approval_result: bool = False
    log_path: Path | None = None
    output_dir: str = ""


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
# SSE log-tailing endpoint
# ---------------------------------------------------------------------------


@app.route("/api/run/status")
def run_status():
    """Stream pipeline progress as Server-Sent Events.

    Opens ``output/agent-log.jsonl`` (relative to the repo root), seeks to
    the end, and forwards each new JSON line to the browser as an SSE
    ``data:`` frame.  A synthetic ``pipeline_complete`` event is emitted
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
# Static entry point
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Serve the single-page application shell."""
    return app.send_static_file("index.html")
