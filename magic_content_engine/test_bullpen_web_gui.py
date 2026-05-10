"""
Tests for the Bullpen Web GUI Flask application.

Covers:
- POST /api/run rejects concurrent runs (409)
- POST /api/run validates empty topic (422)
- POST /api/run/approve and /api/run/reject approval gate
- _make_approval_fn blocking/return-value behaviour

Requirements: 3.4, 7.2, 7.3, 15.2

Run:
    python -m pytest magic_content_engine/test_bullpen_web_gui.py -x -q
"""

from __future__ import annotations

import sys
import os
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import path setup
#
# The Flask app lives in scripts/gui/app.py. We add the repo root to sys.path
# so that `import scripts.gui.app` resolves correctly.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import scripts.gui.app as app_module  # noqa: E402
from scripts.gui.app import RunState  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_PAYLOAD = {"topic": "Kiro IDE 1.0 launch", "outputs": ["blog"]}


def _reset_run_state() -> None:
    """Reset the global run state between tests."""
    app_module._run_state = RunState()


@pytest.fixture(autouse=True)
def reset_state():
    """Ensure global run state is clean before and after every test."""
    _reset_run_state()
    yield
    _reset_run_state()


@pytest.fixture()
def client():
    """Flask test client with TESTING mode enabled."""
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# 2.3 — POST /api/run tests
# ---------------------------------------------------------------------------


class TestRunEndpointConcurrency:
    def test_run_endpoint_rejects_concurrent_runs(self, client):
        """Second POST /api/run while one is in progress returns 409."""
        barrier = threading.Barrier(2, timeout=5)
        stop_event = threading.Event()

        def _blocking_thread(run_state, brief):
            try:
                barrier.wait()
                stop_event.wait(timeout=5)
            finally:
                run_state.in_progress = False

        with patch.object(app_module, "pipeline_runner") as mock_pr:
            mock_pr.run_pipeline_thread.side_effect = _blocking_thread

            resp1 = client.post("/api/run", json=VALID_PAYLOAD)
            assert resp1.status_code == 202, resp1.get_json()

            barrier.wait(timeout=5)

            resp2 = client.post("/api/run", json=VALID_PAYLOAD)
            assert resp2.status_code == 409
            body = resp2.get_json()
            assert body["error"] == "conflict"

            stop_event.set()


class TestRunEndpointValidation:
    def test_run_endpoint_validates_empty_topic(self, client):
        """POST /api/run with empty topic string returns 422."""
        with patch.object(app_module, "pipeline_runner") as mock_pr:
            resp = client.post("/api/run", json={"topic": "", "outputs": ["blog"]})
            assert resp.status_code == 422
            body = resp.get_json()
            assert body["error"] == "validation"
            assert "topic" in body["detail"]
            mock_pr.run_pipeline_thread.assert_not_called()

    def test_run_endpoint_validates_whitespace_only_topic(self, client):
        """POST /api/run with whitespace-only topic returns 422."""
        with patch.object(app_module, "pipeline_runner") as mock_pr:
            resp = client.post("/api/run", json={"topic": "   ", "outputs": ["blog"]})
            assert resp.status_code == 422
            mock_pr.run_pipeline_thread.assert_not_called()

    def test_run_endpoint_validates_missing_outputs(self, client):
        """POST /api/run with no outputs returns 422."""
        with patch.object(app_module, "pipeline_runner") as mock_pr:
            resp = client.post("/api/run", json={"topic": "Kiro IDE", "outputs": []})
            assert resp.status_code == 422
            mock_pr.run_pipeline_thread.assert_not_called()

    def test_run_endpoint_valid_payload_returns_202(self, client):
        """POST /api/run with valid payload returns 202 and a run_id."""
        with patch.object(app_module, "pipeline_runner"):
            resp = client.post("/api/run", json=VALID_PAYLOAD)
            assert resp.status_code == 202
            body = resp.get_json()
            assert "run_id" in body
            assert len(body["run_id"]) == 8


# ---------------------------------------------------------------------------
# 3.4 — Approval gate tests
# ---------------------------------------------------------------------------


class TestApprovalGateApprove:
    """test_approval_gate_approve — POST /api/run/approve sets approval_result=True."""

    def test_approve_sets_result_true_and_signals_event(self, client):
        event = threading.Event()
        app_module._run_state.approval_event = event
        app_module._run_state.approval_result = False

        response = client.post("/api/run/approve")

        assert response.status_code == 200
        assert app_module._run_state.approval_result is True
        assert event.is_set()

    def test_approve_returns_409_when_no_gate_waiting(self, client):
        assert app_module._run_state.approval_event is None

        response = client.post("/api/run/approve")

        assert response.status_code == 409
        data = response.get_json()
        assert data["error"] == "conflict"

    def test_approve_response_body(self, client):
        event = threading.Event()
        app_module._run_state.approval_event = event

        response = client.post("/api/run/approve")
        data = response.get_json()

        assert response.status_code == 200
        assert data["status"] == "approved"


class TestApprovalGateReject:
    """test_approval_gate_reject — POST /api/run/reject sets approval_result=False."""

    def test_reject_sets_result_false_and_signals_event(self, client):
        event = threading.Event()
        app_module._run_state.approval_event = event
        app_module._run_state.approval_result = True

        response = client.post("/api/run/reject")

        assert response.status_code == 200
        assert app_module._run_state.approval_result is False
        assert event.is_set()

    def test_reject_returns_409_when_no_gate_waiting(self, client):
        assert app_module._run_state.approval_event is None

        response = client.post("/api/run/reject")

        assert response.status_code == 409
        data = response.get_json()
        assert data["error"] == "conflict"

    def test_reject_response_body(self, client):
        event = threading.Event()
        app_module._run_state.approval_event = event

        response = client.post("/api/run/reject")
        data = response.get_json()

        assert response.status_code == 200
        assert data["status"] == "rejected"


# ---------------------------------------------------------------------------
# Simple run_state stub for approval_fn tests
# ---------------------------------------------------------------------------


class _SimpleRunState:
    """Minimal run_state stub — avoids MagicMock attribute interception."""

    def __init__(self, approval_result: bool = False) -> None:
        self.approval_event: threading.Event | None = None
        self.approval_result: bool = approval_result
        self.in_progress: bool = True
        self.run_id: str = "test-run"
        self.log_path = None
        self.output_dir: str = "output"


# ---------------------------------------------------------------------------
# _make_approval_fn unit tests
# ---------------------------------------------------------------------------


class TestMakeApprovalFn:
    """Unit tests for the _make_approval_fn factory in pipeline_runner."""

    def test_approval_fn_blocks_until_event_set_and_returns_true(self):
        from scripts.gui.pipeline_runner import _make_approval_fn

        run_state = _SimpleRunState(approval_result=False)
        approval_fn = _make_approval_fn(run_state)

        def _approve():
            import time
            for _ in range(100):
                if run_state.approval_event is not None:
                    break
                time.sleep(0.005)
            run_state.approval_result = True
            run_state.approval_event.set()

        t = threading.Thread(target=_approve, daemon=True)
        t.start()

        result = approval_fn(None)
        t.join(timeout=2)

        assert result is True

    def test_approval_fn_returns_false_when_rejected(self):
        from scripts.gui.pipeline_runner import _make_approval_fn

        run_state = _SimpleRunState(approval_result=True)
        approval_fn = _make_approval_fn(run_state)

        def _reject():
            import time
            for _ in range(100):
                if run_state.approval_event is not None:
                    break
                time.sleep(0.005)
            run_state.approval_result = False
            run_state.approval_event.set()

        t = threading.Thread(target=_reject, daemon=True)
        t.start()

        result = approval_fn(None)
        t.join(timeout=2)

        assert result is False

    def test_approval_fn_creates_new_event_on_run_state(self):
        from scripts.gui.pipeline_runner import _make_approval_fn

        run_state = _SimpleRunState(approval_result=True)
        approval_fn = _make_approval_fn(run_state)

        captured_event: list[threading.Event] = []

        def _unblock():
            import time
            for _ in range(100):
                if run_state.approval_event is not None:
                    break
                time.sleep(0.005)
            captured_event.append(run_state.approval_event)
            run_state.approval_event.set()

        t = threading.Thread(target=_unblock, daemon=True)
        t.start()
        approval_fn(None)
        t.join(timeout=2)

        assert len(captured_event) == 1
        assert isinstance(captured_event[0], threading.Event)
