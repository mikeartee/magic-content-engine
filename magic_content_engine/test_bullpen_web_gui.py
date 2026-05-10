"""
Unit tests for the Bullpen Web GUI.

Tests live here (in magic_content_engine/) so they are picked up by the
existing pytest configuration. All AWS calls are mocked — no credentials
required.

Run:
    python -m pytest magic_content_engine/test_bullpen_web_gui.py -x -q
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """Return a Flask test client with a fresh RunState for each test."""
    # Import here so the module-level singletons are accessible
    import scripts.gui.app as app_module

    # Reset global run state before each test
    app_module._run_state = app_module.RunState()

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c, app_module


# ---------------------------------------------------------------------------
# Task 3.4 — Approval gate tests
# ---------------------------------------------------------------------------


class TestApprovalGateApprove:
    """test_approval_gate_approve — POST /api/run/approve sets approval_result=True."""

    def test_approve_sets_result_true_and_signals_event(self, client):
        test_client, app_module = client

        # Set up a waiting approval event (simulates pipeline thread at gate)
        event = threading.Event()
        app_module._run_state.approval_event = event
        app_module._run_state.approval_result = False

        response = test_client.post("/api/run/approve")

        assert response.status_code == 200
        assert app_module._run_state.approval_result is True
        assert event.is_set(), "approval_event should be set after approve"

    def test_approve_returns_409_when_no_gate_waiting(self, client):
        test_client, app_module = client

        # No approval_event set — gate is not waiting
        assert app_module._run_state.approval_event is None

        response = test_client.post("/api/run/approve")

        assert response.status_code == 409
        data = response.get_json()
        assert data["error"] == "conflict"

    def test_approve_response_body(self, client):
        test_client, app_module = client

        event = threading.Event()
        app_module._run_state.approval_event = event

        response = test_client.post("/api/run/approve")
        data = response.get_json()

        assert response.status_code == 200
        assert data["status"] == "approved"


class TestApprovalGateReject:
    """test_approval_gate_reject — POST /api/run/reject sets approval_result=False."""

    def test_reject_sets_result_false_and_signals_event(self, client):
        test_client, app_module = client

        # Set up a waiting approval event
        event = threading.Event()
        app_module._run_state.approval_event = event
        app_module._run_state.approval_result = True  # start True, expect False after reject

        response = test_client.post("/api/run/reject")

        assert response.status_code == 200
        assert app_module._run_state.approval_result is False
        assert event.is_set(), "approval_event should be set after reject"

    def test_reject_returns_409_when_no_gate_waiting(self, client):
        test_client, app_module = client

        assert app_module._run_state.approval_event is None

        response = test_client.post("/api/run/reject")

        assert response.status_code == 409
        data = response.get_json()
        assert data["error"] == "conflict"

    def test_reject_response_body(self, client):
        test_client, app_module = client

        event = threading.Event()
        app_module._run_state.approval_event = event

        response = test_client.post("/api/run/reject")
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
        """approval_fn should block until the event is set, then return approval_result=True."""
        from scripts.gui.pipeline_runner import _make_approval_fn

        run_state = _SimpleRunState(approval_result=False)
        approval_fn = _make_approval_fn(run_state)

        # Unblock the gate from a helper thread, simulating the approve endpoint
        def _approve():
            import time
            # Wait until approval_fn has created and assigned the event
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
        """approval_fn returns False when approval_result is False."""
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
        """approval_fn should assign a new threading.Event to run_state.approval_event."""
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
