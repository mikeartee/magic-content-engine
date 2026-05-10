"""
Bullpen Web GUI — Flask application.

All API endpoints and the Flask app instance live here.
Blueprints will be registered as each feature area is implemented.
"""

import os
from datetime import date, timedelta

import boto3
from botocore.exceptions import ClientError
from flask import Flask, jsonify

app = Flask(__name__, static_folder="static", static_url_path="/static")

# ---------------------------------------------------------------------------
# AWS region helper
# ---------------------------------------------------------------------------

_DEFAULT_REGION = "ap-southeast-2"


def _aws_region() -> str:
    """Return the AWS region from environment, defaulting to ap-southeast-2."""
    return os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION") or _DEFAULT_REGION


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.route("/api/health")
def health():
    """Simple liveness probe."""
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Topic suggestions — GET /api/suggestions
# ---------------------------------------------------------------------------


@app.route("/api/suggestions")
def get_suggestions():
    """Return up to 10 topics not covered in the last 30 days.

    Topics are ordered by days-since-last-coverage descending — topics
    never covered appear first (days_since=null), then topics covered
    longest ago.

    On any DynamoDB failure, returns 200 with an empty suggestions list
    and a warning message.
    """
    try:
        dynamodb = boto3.resource("dynamodb", region_name=_aws_region())
        table = dynamodb.Table("mce-topic-coverage")
        response = table.scan()
        items = response.get("Items", [])

        # Handle DynamoDB pagination
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
            # Never covered — goes first
            never_covered.append({"topic": topic, "last_covered": None, "days_since": None})
        else:
            try:
                last_covered = date.fromisoformat(str(raw_date))
            except ValueError:
                # Unparseable date — treat as never covered
                never_covered.append({"topic": topic, "last_covered": None, "days_since": None})
                continue

            if last_covered <= cutoff:
                days_since = (today - last_covered).days
                stale.append({
                    "topic": topic,
                    "last_covered": last_covered.isoformat(),
                    "days_since": days_since,
                })

    # Sort stale by days_since descending (longest ago first)
    stale.sort(key=lambda x: x["days_since"], reverse=True)

    suggestions = (never_covered + stale)[:10]
    return jsonify({"suggestions": suggestions})


# ---------------------------------------------------------------------------
# Static entry point
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Serve the single-page application shell."""
    return app.send_static_file("index.html")
