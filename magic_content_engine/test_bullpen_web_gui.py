"""Unit tests for the Bullpen Web GUI Flask application.

All AWS calls are mocked — no real credentials required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Import the Flask app under test
import sys
import os

# Ensure scripts/ is on the path so `scripts.gui.app` can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from gui.app import app as flask_app


@pytest.fixture()
def client():
    """Flask test client with testing mode enabled."""
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# 7.2 — test_suggestions_dynamodb_failure
# ---------------------------------------------------------------------------


def _make_client_error(code: str = "ResourceNotFoundException", message: str = "Table not found") -> ClientError:
    """Build a botocore ClientError for use in mocks."""
    return ClientError(
        error_response={"Error": {"Code": code, "Message": message}},
        operation_name="Scan",
    )


class TestSuggestionsDynamoDBFailure:
    """Verify graceful degradation when DynamoDB is unavailable."""

    def test_dynamodb_client_error_returns_200(self, client):
        """A ClientError from DynamoDB must return HTTP 200."""
        with patch("gui.app.boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.scan.side_effect = _make_client_error()
            mock_resource.return_value.Table.return_value = mock_table

            response = client.get("/api/suggestions")

        assert response.status_code == 200

    def test_dynamodb_client_error_returns_empty_suggestions(self, client):
        """On ClientError, suggestions list must be empty."""
        with patch("gui.app.boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.scan.side_effect = _make_client_error()
            mock_resource.return_value.Table.return_value = mock_table

            response = client.get("/api/suggestions")

        data = response.get_json()
        assert data["suggestions"] == []

    def test_dynamodb_client_error_returns_non_empty_warning(self, client):
        """On ClientError, response must include a non-empty warning field."""
        with patch("gui.app.boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.scan.side_effect = _make_client_error(message="Table not found")
            mock_resource.return_value.Table.return_value = mock_table

            response = client.get("/api/suggestions")

        data = response.get_json()
        assert "warning" in data
        assert data["warning"]  # non-empty string

    def test_dynamodb_generic_exception_returns_200_with_warning(self, client):
        """Any non-ClientError exception also triggers graceful degradation."""
        with patch("gui.app.boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.scan.side_effect = Exception("Network timeout")
            mock_resource.return_value.Table.return_value = mock_table

            response = client.get("/api/suggestions")

        assert response.status_code == 200
        data = response.get_json()
        assert data["suggestions"] == []
        assert data["warning"]

    def test_warning_contains_error_message(self, client):
        """The warning field should include the original error message."""
        with patch("gui.app.boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.scan.side_effect = _make_client_error(message="Access denied")
            mock_resource.return_value.Table.return_value = mock_table

            response = client.get("/api/suggestions")

        data = response.get_json()
        assert "Access denied" in data["warning"]


# ---------------------------------------------------------------------------
# Smoke tests — happy path (no real DynamoDB needed)
# ---------------------------------------------------------------------------


class TestSuggestionsHappyPath:
    """Verify correct filtering and ordering when DynamoDB returns data."""

    def _mock_scan(self, items: list[dict]):
        """Return a mock DynamoDB table whose scan() returns the given items."""
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": items}
        mock_resource = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        return mock_resource

    def test_never_covered_topics_appear_first(self, client):
        """Topics with no last_covered_date should appear before stale ones."""
        from datetime import date, timedelta

        old_date = (date.today() - timedelta(days=60)).isoformat()
        items = [
            {"topic": "Stale Topic", "last_covered_date": old_date},
            {"topic": "Never Covered"},
        ]
        with patch("gui.app.boto3.resource", self._mock_scan(items)):
            response = client.get("/api/suggestions")

        data = response.get_json()
        suggestions = data["suggestions"]
        assert suggestions[0]["topic"] == "Never Covered"
        assert suggestions[0]["last_covered"] is None
        assert suggestions[0]["days_since"] is None

    def test_recently_covered_topics_excluded(self, client):
        """Topics covered within the last 30 days must not appear."""
        from datetime import date, timedelta

        recent_date = (date.today() - timedelta(days=5)).isoformat()
        items = [
            {"topic": "Recent Topic", "last_covered_date": recent_date},
        ]
        with patch("gui.app.boto3.resource", self._mock_scan(items)):
            response = client.get("/api/suggestions")

        data = response.get_json()
        assert data["suggestions"] == []

    def test_returns_at_most_10_suggestions(self, client):
        """Response must contain no more than 10 suggestions."""
        items = [{"topic": f"Topic {i}"} for i in range(20)]
        with patch("gui.app.boto3.resource", self._mock_scan(items)):
            response = client.get("/api/suggestions")

        data = response.get_json()
        assert len(data["suggestions"]) <= 10

    def test_stale_topics_ordered_by_days_since_descending(self, client):
        """Stale topics should be ordered oldest-first (most days since coverage)."""
        from datetime import date, timedelta

        items = [
            {"topic": "Covered 40 days ago", "last_covered_date": (date.today() - timedelta(days=40)).isoformat()},
            {"topic": "Covered 90 days ago", "last_covered_date": (date.today() - timedelta(days=90)).isoformat()},
            {"topic": "Covered 50 days ago", "last_covered_date": (date.today() - timedelta(days=50)).isoformat()},
        ]
        with patch("gui.app.boto3.resource", self._mock_scan(items)):
            response = client.get("/api/suggestions")

        data = response.get_json()
        suggestions = data["suggestions"]
        days = [s["days_since"] for s in suggestions]
        assert days == sorted(days, reverse=True)

    def test_response_shape_matches_spec(self, client):
        """Each suggestion must have topic, last_covered, and days_since fields."""
        from datetime import date, timedelta

        old_date = (date.today() - timedelta(days=45)).isoformat()
        items = [
            {"topic": "Stale Topic", "last_covered_date": old_date},
            {"topic": "Never Covered"},
        ]
        with patch("gui.app.boto3.resource", self._mock_scan(items)):
            response = client.get("/api/suggestions")

        data = response.get_json()
        assert "suggestions" in data
        for s in data["suggestions"]:
            assert "topic" in s
            assert "last_covered" in s
            assert "days_since" in s
