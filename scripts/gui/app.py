"""
Bullpen Web GUI — Flask application.

All API endpoints and the Flask app instance live here.
Blueprints will be registered as each feature area is implemented.
"""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from flask import Flask, Response, jsonify, request, send_file, stream_with_context

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
# Path helpers
# ---------------------------------------------------------------------------

# Repo root is the parent of the `scripts/` directory that contains this file.
# scripts/gui/app.py  ->  scripts/  ->  repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_OUTPUT_DIR = _REPO_ROOT / "output"

# Files that are internal to the pipeline and should not be surfaced in the
# file listings returned to the GUI.
_EXCLUDED_FILES = {"agent-log.jsonl", "checkpoints.json"}


def _run_dir(run_id: str) -> Path:
    """Return the absolute path for a run bundle directory."""
    return _OUTPUT_DIR / run_id


def _safe_file_path(run_id: str, filename: str) -> Path | None:
    """
    Resolve *filename* inside the run bundle for *run_id*.

    Returns the resolved Path if it stays within the run bundle directory,
    or None if the resolved path escapes (path traversal attempt).
    """
    base = _run_dir(run_id).resolve()
    candidate = (base / filename).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


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


# ---------------------------------------------------------------------------
# 6.1  GET /api/runs
# ---------------------------------------------------------------------------


@app.route("/api/runs")
def list_runs():
    """
    List all run bundle directories under output/, sorted descending by name.

    Response shape:
        {"runs": [{"id": "2026-05-10-weekly-update", "files": ["post.md", ...]}, ...]}
    """
    if not _OUTPUT_DIR.is_dir():
        return jsonify({"runs": []})

    runs = []
    for entry in sorted(_OUTPUT_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        if not entry.is_dir():
            continue
        files = sorted(
            f.name
            for f in entry.iterdir()
            if f.is_file() and f.name not in _EXCLUDED_FILES
        )
        runs.append({"id": entry.name, "files": files})

    return jsonify({"runs": runs})


# ---------------------------------------------------------------------------
# 6.2  GET /api/runs/<run_id>/file?name=<filename>
# ---------------------------------------------------------------------------


@app.route("/api/runs/<run_id>/file", methods=["GET"])
def get_file(run_id: str):
    """
    Return the raw content of a file in a run bundle as text/plain.

    Query parameter:
        name  -- filename within the run bundle (e.g. post.md)

    Returns 400 if the name parameter is missing.
    Returns 403 if the resolved path escapes the run bundle (path traversal).
    Returns 404 if the file does not exist.
    """
    filename = request.args.get("name", "")
    if not filename:
        return jsonify({"error": "missing_parameter", "detail": "name is required"}), 400

    file_path = _safe_file_path(run_id, filename)
    if file_path is None:
        return jsonify({"error": "forbidden", "detail": "path traversal detected"}), 403

    if not file_path.is_file():
        return jsonify({"error": "not_found", "detail": f"{filename} not found in run {run_id}"}), 404

    content = file_path.read_bytes().decode("utf-8")
    return content, 200, {"Content-Type": "text/plain; charset=utf-8"}


# ---------------------------------------------------------------------------
# 6.3  POST /api/runs/<run_id>/file
# ---------------------------------------------------------------------------


@app.route("/api/runs/<run_id>/file", methods=["POST"])
def save_file(run_id: str):
    """
    Atomically write content to a file in a run bundle.

    Request body (JSON):
        {"name": "post.md", "content": "..."}

    Returns 422 if content is empty or missing.
    Returns 403 if the resolved path escapes the run bundle.
    Returns 500 if the atomic rename fails (original file is left unchanged).
    Returns 200 {"saved": true} on success.
    """
    body = request.get_json(silent=True) or {}
    name = body.get("name", "")
    content = body.get("content", "")

    if not name:
        return jsonify({"error": "validation", "detail": "name is required"}), 422

    if not content:
        return jsonify({"error": "validation", "detail": "content must be non-empty"}), 422

    file_path = _safe_file_path(run_id, name)
    if file_path is None:
        return jsonify({"error": "forbidden", "detail": "path traversal detected"}), 403

    # Ensure the run bundle directory exists.
    file_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file in the same directory, then atomically rename.
    tmp_fd = None
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(file_path.parent),
            prefix=".tmp_",
            suffix=".tmp",
        )
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(content.encode("utf-8"))
        tmp_fd = None  # fdopen took ownership; don't double-close

        os.replace(tmp_path, str(file_path))
        tmp_path = None  # replace succeeded; nothing to clean up
    except OSError as exc:
        # Clean up the temp file if it still exists.
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return jsonify({"error": "write_error", "detail": str(exc)}), 500
    finally:
        # Guard against an unclosed fd if an exception occurred before fdopen.
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass

    return jsonify({"saved": True}), 200


# ---------------------------------------------------------------------------
# 6.4  GET /api/runs/<run_id>/download/<filename>
# ---------------------------------------------------------------------------


@app.route("/api/runs/<run_id>/download/<filename>")
def download_file(run_id: str, filename: str):
    """
    Serve a file from a run bundle as an attachment download.

    Returns 403 if the resolved path escapes the run bundle.
    Returns 404 if the file does not exist.
    """
    file_path = _safe_file_path(run_id, filename)
    if file_path is None:
        return jsonify({"error": "forbidden", "detail": "path traversal detected"}), 403

    if not file_path.is_file():
        return jsonify({"error": "not_found", "detail": f"{filename} not found in run {run_id}"}), 404

    return send_file(
        str(file_path),
        as_attachment=True,
        download_name=filename,
        mimetype="text/plain",
    )
