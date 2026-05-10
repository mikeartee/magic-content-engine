"""
Bullpen Web GUI — Flask application.

All API endpoints and the Flask app instance live here.
Blueprints will be registered as each feature area is implemented.
"""

import os
from pathlib import Path

from flask import Flask, jsonify, request

from scripts.gui import devto_client

app = Flask(__name__, static_folder="static", static_url_path="/static")

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
# dev.to publish endpoint
# ---------------------------------------------------------------------------


@app.route("/api/publish/devto", methods=["POST"])
def publish_devto():
    """Publish or draft a post.md to dev.to.

    Request body (JSON):
        {
            "run_id": "2026-05-10-weekly-update",
            "title": "My article title",
            "tags": ["aws", "python"],
            "published": true
        }

    Returns:
        201 — {"success": true, "url": "...", "id": ...}
        400 — DEVTO_API_KEY missing or empty
        404 — post.md not found for the given run_id
        502 — upstream dev.to failure
    """
    api_key = os.environ.get("DEVTO_API_KEY", "")
    if not api_key:
        return jsonify({"error": "missing_api_key", "detail": "DEVTO_API_KEY is not set"}), 400

    data = request.get_json(force=True, silent=True) or {}
    run_id = data.get("run_id", "")
    title = data.get("title", "")
    tags = data.get("tags", [])
    published = data.get("published", False)

    post_path = Path("output") / run_id / "post.md"
    if not post_path.exists():
        return jsonify({"error": "not_found", "detail": f"post.md not found for run {run_id!r}"}), 404

    body_markdown = post_path.read_text(encoding="utf-8")

    result = devto_client.publish_article(api_key, title, body_markdown, tags, published)

    if result.get("success"):
        return jsonify(result), 201

    return jsonify(result), 502
