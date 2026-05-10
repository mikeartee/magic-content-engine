"""Tests for the Bullpen Web GUI Flask application.

Covers:
- POST /api/run rejects concurrent runs (409)
- POST /api/run validates empty topic (422)

Requirements: 3.4, 15.2
"""

from __future__ import annotations

import sys
import os
import threading
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import path setup
#
# The Flask app lives in scripts/gui/app.py. scripts/ has no __init__.py so
# it is not a regular package. We add the scripts/gui directory to sys.path
# so that `import app` and `import pipeline_runner` resolve correctly when
# the test runner imports this file.
# ---------------------------------------------------------------------------

_WORKTREE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI_DIR = os.path.join(_WORKTREE_ROOT, "scripts", "gui")

if _GUI_DIR not in sys.path:
    sys.path.insert(0, _GUI_DIR)

# Now import the Flask app. pipeline_runner is mocked in each test so the
# stub's run_pipeline_thread is never actually called.
import app as gui_app  # noqa: E402  (import after sys.path manipulation)
from app import RunState, _run_state, _run_lock  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_PAYLOAD = {"topic": "Kiro IDE 1.0 launch", "outputs": ["blog"]}


def _reset_run_state() -> None:
    """Reset the global run state between tests."""
    with _run_lock:
        _run_state.in_progress = False
        _run_state.run_id = None
        _run_state.approval_event = None
        _run_state.approval_result = None
        _run_state.log_path = None
        _run_state.output_dir = None


@pytest.fixture(autouse=True)
def reset_state():
    """Ensure global run state is clean before and after every test."""
    _reset_run_state()
    yield
    _reset_run_state()


@pytest.fixture()
def client():
    """Flask test client with TESTING mode enabled."""
    gui_app.app.config["TESTING"] = True
    with gui_app.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# 2.3 — POST /api/run tests
# ---------------------------------------------------------------------------


class TestRunEndpointConcurrency:
    def test_run_endpoint_rejects_concurrent_runs(self, client):
        """Second POST /api/run while one is in progress returns 409."""
        # Patch run_pipeline_thread so it blocks long enough for the second
        # request to arrive, but doesn't actually run the pipeline.
        barrier = threading.Barrier(2, timeout=5)
        stop_event = threading.Event()

        def _blocking_thread(run_state, brief):
            # Signal that the thread has started and is "in progress"
            try:
                barrier.wait()
                # Hold in_progress=True until the test releases us
                stop_event.wait(timeout=5)
            finally:
                run_state.in_progress = False

        with patch("pipeline_runner.run_pipeline_thread", side_effect=_blocking_thread):
            # First request — should succeed (202)
            resp1 = client.post("/api/run", json=VALID_PAYLOAD)
            assert resp1.status_code == 202, resp1.get_json()

            # Wait until the background thread has started and set in_progress
            barrier.wait(timeout=5)

            # Second request — should be rejected (409)
            resp2 = client.post("/api/run", json=VALID_PAYLOAD)
            assert resp2.status_code == 409
            body = resp2.get_json()
            assert body["error"] == "conflict"

            # Release the background thread
            stop_event.set()


class TestRunEndpointValidation:
    def test_run_endpoint_validates_empty_topic(self, client):
        """POST /api/run with empty topic string returns 422."""
        with patch("pipeline_runner.run_pipeline_thread") as mock_thread:
            resp = client.post("/api/run", json={"topic": "", "outputs": ["blog"]})
            assert resp.status_code == 422
            body = resp.get_json()
            assert body["error"] == "validation"
            assert "topic" in body["detail"]
            # Pipeline must not have been started
            mock_thread.assert_not_called()

    def test_run_endpoint_validates_whitespace_only_topic(self, client):
        """POST /api/run with whitespace-only topic returns 422."""
        with patch("pipeline_runner.run_pipeline_thread") as mock_thread:
            resp = client.post("/api/run", json={"topic": "   ", "outputs": ["blog"]})
            assert resp.status_code == 422
            body = resp.get_json()
            assert body["error"] == "validation"
            mock_thread.assert_not_called()

    def test_run_endpoint_validates_missing_outputs(self, client):
        """POST /api/run with no outputs returns 422."""
        with patch("pipeline_runner.run_pipeline_thread") as mock_thread:
            resp = client.post("/api/run", json={"topic": "Kiro IDE", "outputs": []})
            assert resp.status_code == 422
            body = resp.get_json()
            assert body["error"] == "validation"
            mock_thread.assert_not_called()

    def test_run_endpoint_valid_payload_returns_202(self, client):
        """POST /api/run with valid payload returns 202 and a run_id."""
        with patch("pipeline_runner.run_pipeline_thread"):
            resp = client.post("/api/run", json=VALID_PAYLOAD)
            assert resp.status_code == 202
            body = resp.get_json()
            assert "run_id" in body
            assert len(body["run_id"]) == 8  # uuid4().hex[:8]

    def test_run_endpoint_run_id_is_unique(self, client):
        """Each successful POST /api/run returns a distinct run_id."""
        run_ids = []

        def _instant_thread(run_state, brief):
            run_state.in_progress = False

        with patch("pipeline_runner.run_pipeline_thread", side_effect=_instant_thread):
            for _ in range(3):
                _reset_run_state()
                resp = client.post("/api/run", json=VALID_PAYLOAD)
                assert resp.status_code == 202
                run_ids.append(resp.get_json()["run_id"])

        assert len(set(run_ids)) == 3, "run_ids should be unique across calls"
