"""
Bullpen Web GUI — Flask application.

All API endpoints and the Flask app instance live here.
"""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

# Pipeline imports (read-only consumers — pipeline package is not modified)
from magic_content_engine.bullpen.models import BullpenBrief

try:
    from . import pipeline_runner
    from . import log_tailer
    from . import devto_client
except ImportError:
    import pipeline_runner  # type: ignore[no-redef]
    import log_tailer  # type: ignore[no-redef]
    import devto_client  # type: ignore[no-redef]

app = Flask(__name__, static_folder="static", static_url_path="/static")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_OUTPUT_DIR = _REPO_ROOT / "output"
_EXCLUDED_FILES = {"agent-log.jsonl", "checkpoints.json"}
_DEFAULT_REGION = "ap-southeast-2"


def _aws_region() -> str:
    return os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION") or _DEFAULT_REGION


def _run_dir(run_id: str) -> Path:
    return _OUTPUT_DIR / run_id


def _safe_file_path(run_id: str, filename: str) -> Optional[Path]:
    base = _run_dir(run_id).resolve()
    candidate = (base / filename).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


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
    log_path: Optional[Path] = None
    output_dir: Optional[str] = None


_run_state: RunState = RunState()
_run_lock: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# POST /api/run
# ---------------------------------------------------------------------------


@app.route("/api/run", methods=["POST"])
def start_run():
    body = request.get_json(silent=True) or {}
    topic: str = (body.get("topic") or "").strip()
    outputs: list = body.get("outputs") or []

    if not topic:
        return jsonify({"error": "validation", "detail": "topic must be non-empty"}), 422

    if not outputs:
        return jsonify({"error": "validation", "detail": "at least one output type must be selected"}), 422

    with _run_lock:
        if _run_state.in_progress:
            return jsonify({"error": "conflict", "detail": "A pipeline run is already in progress."}), 409

        run_id = uuid.uuid4().hex[:8]
        brief = BullpenBrief(topic=topic, requested_outputs=list(outputs))

        _run_state.in_progress = True
        _run_state.run_id = run_id
        _run_state.approval_event = None
        _run_state.approval_result = None
        _run_state.log_path = None
        _run_state.output_dir = None

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
    generator = log_tailer.tail_log(_run_state)
    response = Response(stream_with_context(generator), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


# ---------------------------------------------------------------------------
# Approval gate endpoints
# ---------------------------------------------------------------------------


@app.route("/api/run/approve", methods=["POST"])
def approve_run():
    with _run_lock:
        event = _run_state.approval_event
        if event is None:
            return jsonify({"error": "conflict", "detail": "No approval gate is currently waiting."}), 409
        _run_state.approval_result = True
        event.set()
    return jsonify({"status": "approved"}), 200


@app.route("/api/run/reject", methods=["POST"])
def reject_run():
    with _run_lock:
        event = _run_state.approval_event
        if event is None:
            return jsonify({"error": "conflict", "detail": "No approval gate is currently waiting."}), 409
        _run_state.approval_result = False
        event.set()
    return jsonify({"status": "rejected"}), 200


# ---------------------------------------------------------------------------
# Run bundle file API
# ---------------------------------------------------------------------------


@app.route("/api/runs")
def list_runs():
    if not _OUTPUT_DIR.is_dir():
        return jsonify({"runs": []})
    runs = []
    for entry in sorted(_OUTPUT_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        if not entry.is_dir():
            continue
        files = sorted(f.name for f in entry.iterdir() if f.is_file() and f.name not in _EXCLUDED_FILES)
        runs.append({"id": entry.name, "files": files})
    return jsonify({"runs": runs})


@app.route("/api/runs/<run_id>/file", methods=["GET"])
def get_file(run_id: str):
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


@app.route("/api/runs/<run_id>/file", methods=["POST"])
def save_file(run_id: str):
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

    file_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_fd = None
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(file_path.parent), prefix=".tmp_", suffix=".tmp")
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(content.encode("utf-8"))
        tmp_fd = None
        os.replace(tmp_path, str(file_path))
        tmp_path = None
    except OSError as exc:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return jsonify({"error": "write_error", "detail": str(exc)}), 500
    finally:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass

    return jsonify({"saved": True}), 200


@app.route("/api/runs/<run_id>/download/<filename>")
def download_file(run_id: str, filename: str):
    file_path = _safe_file_path(run_id, filename)
    if file_path is None:
        return jsonify({"error": "forbidden", "detail": "path traversal detected"}), 403
    if not file_path.is_file():
        return jsonify({"error": "not_found", "detail": f"{filename} not found in run {run_id}"}), 404
    return send_file(str(file_path), as_attachment=True, download_name=filename, mimetype="text/plain")


# ---------------------------------------------------------------------------
# DynamoDB suggestions
# ---------------------------------------------------------------------------


@app.route("/api/suggestions")
def get_suggestions():
    try:
        dynamodb = boto3.resource("dynamodb", region_name=_aws_region())
        table = dynamodb.Table("mce-topic-coverage")
        response = table.scan()
        items = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))
    except ClientError as exc:
        error_msg = exc.response["Error"].get("Message", str(exc))
        return jsonify({"suggestions": [], "warning": f"Could not load suggestions: {error_msg}"})
    except Exception as exc:
        return jsonify({"suggestions": [], "warning": f"Could not load suggestions: {exc}"})

    today = date.today()
    cutoff = today - timedelta(days=30)
    never_covered: list[dict] = []
    stale: list[dict] = []

    for item in items:
        topic = item.get("topic", "")
        raw_date = item.get("last_covered_date")
        if not raw_date:
            never_covered.append({"topic": topic, "last_covered": None, "days_since": None})
        else:
            try:
                last_covered = date.fromisoformat(str(raw_date))
            except ValueError:
                never_covered.append({"topic": topic, "last_covered": None, "days_since": None})
                continue
            if last_covered <= cutoff:
                days_since = (today - last_covered).days
                stale.append({"topic": topic, "last_covered": last_covered.isoformat(), "days_since": days_since})

    stale.sort(key=lambda x: x["days_since"], reverse=True)
    suggestions = (never_covered + stale)[:10]
    return jsonify({"suggestions": suggestions})


# ---------------------------------------------------------------------------
# dev.to publish
# ---------------------------------------------------------------------------


@app.route("/api/publish/devto", methods=["POST"])
def publish_devto():
    api_key = os.environ.get("DEVTO_API_KEY", "")
    if not api_key:
        return jsonify({"error": "missing_api_key", "detail": "DEVTO_API_KEY is not set"}), 400

    data = request.get_json(force=True, silent=True) or {}
    run_id = data.get("run_id", "")
    title = data.get("title", "")
    tags = data.get("tags", [])
    published = data.get("published", False)

    post_path = _OUTPUT_DIR / run_id / "post.md"
    if not post_path.exists():
        return jsonify({"error": "not_found", "detail": f"post.md not found for run {run_id!r}"}), 404

    body_markdown = post_path.read_text(encoding="utf-8")
    result = devto_client.publish_article(api_key, title, body_markdown, tags, published)

    if result.get("success"):
        return jsonify(result), 201
    return jsonify(result), 502


# ---------------------------------------------------------------------------
# Static entry point
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return app.send_static_file("index.html")
