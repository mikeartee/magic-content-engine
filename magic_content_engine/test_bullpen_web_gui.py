"""
Tests for the Bullpen Web GUI Flask application.

Covers:
- POST /api/run rejects concurrent runs (409)
- POST /api/run validates empty topic (422)
- POST /api/run/approve and /api/run/reject approval gate
- _make_approval_fn blocking/return-value behaviour
- 6.5  Unit test: test_file_save_atomic
- 6.6  Property test: round-trip file save (Hypothesis)

Requirements: 3.4, 7.2, 7.3, 15.2

Run:
    python -m pytest magic_content_engine/test_bullpen_web_gui.py -x -q
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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


def _make_run(output_dir: Path, run_id: str, files: dict[str, str]) -> Path:
    """Create a run bundle directory with the given files.

    Files are written in binary mode (UTF-8 encoded) to avoid platform
    line-ending translation, matching the behaviour of the save endpoint.
    """
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (run_dir / name).write_bytes(content.encode("utf-8"))
    return run_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_state():
    """Ensure global run state is clean before and after every test."""
    _reset_run_state()
    yield
    _reset_run_state()


@pytest.fixture()
def client():
    """Flask test client with TESTING mode enabled.

    Used by run/approval tests that do not need a patched _OUTPUT_DIR.
    Yields just the test client ``c``.
    """
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture()
def file_client(tmp_path):
    """Flask test client with _OUTPUT_DIR patched to a temporary directory.

    Used by file API tests. Each test gets an isolated output directory so
    tests never touch the real output/ folder and never interfere with each
    other. Yields ``(c, tmp_path)``.
    """
    app_module.app.config["TESTING"] = True
    with patch.object(app_module, "_OUTPUT_DIR", tmp_path):
        with app_module.app.test_client() as c:
            yield c, tmp_path


# ---------------------------------------------------------------------------
# 2.3 -- POST /api/run tests
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
# 3.4 -- Approval gate tests
# ---------------------------------------------------------------------------


class TestApprovalGateApprove:
    """test_approval_gate_approve -- POST /api/run/approve sets approval_result=True."""

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
    """test_approval_gate_reject -- POST /api/run/reject sets approval_result=False."""

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
    """Minimal run_state stub -- avoids MagicMock attribute interception."""

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


# ---------------------------------------------------------------------------
# 6.5  Unit test: test_file_save_atomic
# ---------------------------------------------------------------------------


class TestFileSaveAtomic:
    """Tests for the atomic file-save endpoint (POST /api/runs/<run_id>/file)."""

    def test_save_creates_file(self, file_client):
        """Saving a new file creates it with the correct content."""
        c, output_dir = file_client
        run_id = "2026-01-01-test"
        _make_run(output_dir, run_id, {})

        resp = c.post(
            f"/api/runs/{run_id}/file",
            data=json.dumps({"name": "post.md", "content": "# Hello\n"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {"saved": True}

        saved = (output_dir / run_id / "post.md").read_text(encoding="utf-8")
        assert saved == "# Hello\n"

    def test_save_updates_existing_file(self, file_client):
        """Saving over an existing file replaces its content."""
        c, output_dir = file_client
        run_id = "2026-01-02-test"
        _make_run(output_dir, run_id, {"post.md": "original content"})

        resp = c.post(
            f"/api/runs/{run_id}/file",
            data=json.dumps({"name": "post.md", "content": "updated content"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        saved = (output_dir / run_id / "post.md").read_text(encoding="utf-8")
        assert saved == "updated content"

    def test_save_empty_content_returns_422(self, file_client):
        """Empty content is rejected with 422."""
        c, output_dir = file_client
        run_id = "2026-01-03-test"
        _make_run(output_dir, run_id, {})

        resp = c.post(
            f"/api/runs/{run_id}/file",
            data=json.dumps({"name": "post.md", "content": ""}),
            content_type="application/json",
        )
        assert resp.status_code == 422

    def test_save_missing_content_returns_422(self, file_client):
        """Missing content field is rejected with 422."""
        c, output_dir = file_client
        run_id = "2026-01-04-test"
        _make_run(output_dir, run_id, {})

        resp = c.post(
            f"/api/runs/{run_id}/file",
            data=json.dumps({"name": "post.md"}),
            content_type="application/json",
        )
        assert resp.status_code == 422

    def test_save_rename_failure_returns_500_and_original_unchanged(self, file_client):
        """
        When os.rename raises OSError the endpoint returns 500 and the
        original file is left unchanged.
        """
        c, output_dir = file_client
        run_id = "2026-01-05-test"
        original_content = "original content -- must survive"
        _make_run(output_dir, run_id, {"post.md": original_content})

        with patch("os.replace", side_effect=OSError("simulated rename failure")):
            resp = c.post(
                f"/api/runs/{run_id}/file",
                data=json.dumps({"name": "post.md", "content": "new content"}),
                content_type="application/json",
            )

        assert resp.status_code == 500
        data = resp.get_json()
        assert "error" in data

        # Original file must be unchanged.
        surviving = (output_dir / run_id / "post.md").read_bytes().decode("utf-8")
        assert surviving == original_content

    def test_save_path_traversal_rejected(self, file_client):
        """Path traversal attempts are rejected with 403."""
        c, output_dir = file_client
        run_id = "2026-01-06-test"
        _make_run(output_dir, run_id, {})

        resp = c.post(
            f"/api/runs/{run_id}/file",
            data=json.dumps({"name": "../../../etc/passwd", "content": "evil"}),
            content_type="application/json",
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 6.5  Additional unit tests for GET endpoints
# ---------------------------------------------------------------------------


class TestListRuns:
    """Tests for GET /api/runs."""

    def test_empty_output_dir(self, file_client):
        """Returns empty list when output dir has no subdirectories."""
        c, output_dir = file_client
        resp = c.get("/api/runs")
        assert resp.status_code == 200
        assert resp.get_json() == {"runs": []}

    def test_lists_runs_sorted_descending(self, file_client):
        """Run bundles are returned sorted by name descending."""
        c, output_dir = file_client
        _make_run(output_dir, "2026-01-01-alpha", {"post.md": "a"})
        _make_run(output_dir, "2026-01-03-gamma", {"post.md": "c"})
        _make_run(output_dir, "2026-01-02-beta", {"post.md": "b"})

        resp = c.get("/api/runs")
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.get_json()["runs"]]
        assert ids == ["2026-01-03-gamma", "2026-01-02-beta", "2026-01-01-alpha"]

    def test_excludes_internal_files(self, file_client):
        """agent-log.jsonl and checkpoints.json are excluded from file lists."""
        c, output_dir = file_client
        _make_run(
            output_dir,
            "2026-02-01-test",
            {
                "post.md": "content",
                "agent-log.jsonl": "{}",
                "checkpoints.json": "{}",
                "script.md": "script",
            },
        )

        resp = c.get("/api/runs")
        run = resp.get_json()["runs"][0]
        assert "agent-log.jsonl" not in run["files"]
        assert "checkpoints.json" not in run["files"]
        assert "post.md" in run["files"]
        assert "script.md" in run["files"]

    def test_ignores_files_at_output_root(self, file_client):
        """Files directly in output/ (not in subdirs) are not listed as runs."""
        c, output_dir = file_client
        (output_dir / "agent-log.jsonl").write_text("{}", encoding="utf-8")
        _make_run(output_dir, "2026-03-01-real-run", {"post.md": "x"})

        resp = c.get("/api/runs")
        ids = [r["id"] for r in resp.get_json()["runs"]]
        assert ids == ["2026-03-01-real-run"]


class TestGetFile:
    """Tests for GET /api/runs/<run_id>/file."""

    def test_returns_file_content(self, file_client):
        """Returns the raw file content as text/plain."""
        c, output_dir = file_client
        _make_run(output_dir, "2026-04-01-test", {"post.md": "# My Post\n"})

        resp = c.get("/api/runs/2026-04-01-test/file?name=post.md")
        assert resp.status_code == 200
        assert resp.data.decode("utf-8") == "# My Post\n"
        assert "text/plain" in resp.content_type

    def test_404_for_missing_file(self, file_client):
        """Returns 404 when the file does not exist."""
        c, output_dir = file_client
        _make_run(output_dir, "2026-04-02-test", {})

        resp = c.get("/api/runs/2026-04-02-test/file?name=missing.md")
        assert resp.status_code == 404

    def test_400_for_missing_name_param(self, file_client):
        """Returns 400 when the name query parameter is absent."""
        c, output_dir = file_client
        _make_run(output_dir, "2026-04-03-test", {})

        resp = c.get("/api/runs/2026-04-03-test/file")
        assert resp.status_code == 400

    def test_path_traversal_rejected(self, file_client):
        """Path traversal in the name parameter is rejected with 403."""
        c, output_dir = file_client
        _make_run(output_dir, "2026-04-04-test", {})

        resp = c.get("/api/runs/2026-04-04-test/file?name=../../secrets.txt")
        assert resp.status_code == 403


class TestDownloadFile:
    """Tests for GET /api/runs/<run_id>/download/<filename>."""

    def test_serves_file_as_attachment(self, file_client):
        """Returns the file with Content-Disposition: attachment."""
        c, output_dir = file_client
        _make_run(output_dir, "2026-05-01-test", {"script.md": "# Script\n"})

        resp = c.get("/api/runs/2026-05-01-test/download/script.md")
        assert resp.status_code == 200
        assert "attachment" in resp.headers.get("Content-Disposition", "")
        assert "script.md" in resp.headers.get("Content-Disposition", "")

    def test_404_for_missing_file(self, file_client):
        """Returns 404 when the file does not exist."""
        c, output_dir = file_client
        _make_run(output_dir, "2026-05-02-test", {})

        resp = c.get("/api/runs/2026-05-02-test/download/missing.md")
        assert resp.status_code == 404

    def test_path_traversal_rejected(self, file_client):
        """Path traversal in the filename segment is rejected with 403."""
        c, output_dir = file_client
        _make_run(output_dir, "2026-05-03-test", {})

        resp = c.get("/api/runs/2026-05-03-test/download/../../secrets.txt")
        # Flask may normalise the URL before it reaches the view; either 403
        # or 404 is acceptable -- the important thing is it does NOT serve the
        # file outside the run bundle.
        assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# 6.6  Property test: round-trip file save
# ---------------------------------------------------------------------------


class TestRoundTripFileSave:
    """
    Property: for any non-empty string, saving via POST and reading back via
    GET produces the identical string.
    """

    @given(st.text(min_size=1))
    @settings(max_examples=100)
    def test_round_trip(self, content: str):
        """
        Saving arbitrary non-empty text and reading it back returns the same
        string, regardless of Unicode content, newlines, or special characters.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app_module.app.config["TESTING"] = True

            with patch.object(app_module, "_OUTPUT_DIR", tmp_path):
                run_id = "prop-test-run"
                _make_run(tmp_path, run_id, {})

                with app_module.app.test_client() as c:
                    # Save via POST
                    save_resp = c.post(
                        f"/api/runs/{run_id}/file",
                        data=json.dumps({"name": "post.md", "content": content}),
                        content_type="application/json",
                    )
                    assert save_resp.status_code == 200, (
                        f"Save failed with {save_resp.status_code}: {save_resp.data}"
                    )

                    # Read back via GET
                    get_resp = c.get(f"/api/runs/{run_id}/file?name=post.md")
                    assert get_resp.status_code == 200

                    returned = get_resp.data.decode("utf-8")
                    assert returned == content, (
                        f"Round-trip mismatch.\n"
                        f"  Input:    {content!r}\n"
                        f"  Returned: {returned!r}"
                    )
