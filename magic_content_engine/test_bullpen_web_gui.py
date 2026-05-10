"""
Unit tests for the Bullpen Web GUI Flask application.

Uses Flask's built-in test client. No AWS credentials or live network calls
are required — dev.to and DynamoDB calls are mocked.
"""

import os
import pytest

# ---------------------------------------------------------------------------
# Task 8.3 — test_devto_publish_missing_api_key
# ---------------------------------------------------------------------------


def test_devto_publish_missing_api_key(monkeypatch, tmp_path):
    """POST /api/publish/devto returns 400 when DEVTO_API_KEY is empty."""
    # Ensure the env var is absent / empty before importing the app so that
    # any module-level reads also see the empty value.
    monkeypatch.setenv("DEVTO_API_KEY", "")

    # Import app inside the test so monkeypatch is already applied.
    from scripts.gui.app import app

    with app.test_client() as client:
        response = client.post(
            "/api/publish/devto",
            json={
                "run_id": "2026-05-10-weekly-update",
                "title": "Test article",
                "tags": ["aws", "python"],
                "published": True,
            },
        )

    assert response.status_code == 400
    body = response.get_json()
    assert body["error"] == "missing_api_key"
