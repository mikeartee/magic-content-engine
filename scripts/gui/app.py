"""
Bullpen Web GUI — Flask application.

All API endpoints and the Flask app instance live here.
Blueprints will be registered as each feature area is implemented.
"""

from flask import Flask, jsonify

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
