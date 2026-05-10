"""
Bullpen Web GUI — Flask application.

All API endpoints and the Flask app instance live here.
Blueprints will be registered as each feature area is implemented.
"""

import os
import tempfile
from pathlib import Path

from flask import Flask, jsonify, request, send_file

app = Flask(__name__, static_folder="static", static_url_path="/static")

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

# Repo root is the parent of the `scripts/` directory that contains this file.
# scripts/gui/app.py  →  scripts/  →  repo root
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
        name  — filename within the run bundle (e.g. post.md)

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
