"""
Tests for the Bullpen Web GUI file API endpoints.

Covers:
  - 6.5  Unit test: test_file_save_atomic
  - 6.6  Property test: round-trip file save (Hypothesis)
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Make sure the scripts/ directory is importable so we can import app.py
# without installing the package.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from gui.app import app as flask_app  # noqa: E402  (import after sys.path tweak)
from gui import app as app_module  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    """
    Flask test client with _OUTPUT_DIR patched to a temporary directory.

    Each test gets an isolated output directory so tests never touch the real
    output/ folder and never interfere with each other.
    """
    flask_app.config["TESTING"] = True
    with patch.object(app_module, "_OUTPUT_DIR", tmp_path):
        with flask_app.test_client() as c:
            yield c, tmp_path


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


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
# 6.5  Unit test: test_file_save_atomic
# ---------------------------------------------------------------------------


class TestFileSaveAtomic:
    """Tests for the atomic file-save endpoint (POST /api/runs/<run_id>/file)."""

    def test_save_creates_file(self, client):
        """Saving a new file creates it with the correct content."""
        c, output_dir = client
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

    def test_save_updates_existing_file(self, client):
        """Saving over an existing file replaces its content."""
        c, output_dir = client
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

    def test_save_empty_content_returns_422(self, client):
        """Empty content is rejected with 422."""
        c, output_dir = client
        run_id = "2026-01-03-test"
        _make_run(output_dir, run_id, {})

        resp = c.post(
            f"/api/runs/{run_id}/file",
            data=json.dumps({"name": "post.md", "content": ""}),
            content_type="application/json",
        )
        assert resp.status_code == 422

    def test_save_missing_content_returns_422(self, client):
        """Missing content field is rejected with 422."""
        c, output_dir = client
        run_id = "2026-01-04-test"
        _make_run(output_dir, run_id, {})

        resp = c.post(
            f"/api/runs/{run_id}/file",
            data=json.dumps({"name": "post.md"}),
            content_type="application/json",
        )
        assert resp.status_code == 422

    def test_save_rename_failure_returns_500_and_original_unchanged(self, client):
        """
        When os.rename raises OSError the endpoint returns 500 and the
        original file is left unchanged.
        """
        c, output_dir = client
        run_id = "2026-01-05-test"
        original_content = "original content — must survive"
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

    def test_save_path_traversal_rejected(self, client):
        """Path traversal attempts are rejected with 403."""
        c, output_dir = client
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

    def test_empty_output_dir(self, client):
        """Returns empty list when output dir has no subdirectories."""
        c, output_dir = client
        resp = c.get("/api/runs")
        assert resp.status_code == 200
        assert resp.get_json() == {"runs": []}

    def test_lists_runs_sorted_descending(self, client):
        """Run bundles are returned sorted by name descending."""
        c, output_dir = client
        _make_run(output_dir, "2026-01-01-alpha", {"post.md": "a"})
        _make_run(output_dir, "2026-01-03-gamma", {"post.md": "c"})
        _make_run(output_dir, "2026-01-02-beta", {"post.md": "b"})

        resp = c.get("/api/runs")
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.get_json()["runs"]]
        assert ids == ["2026-01-03-gamma", "2026-01-02-beta", "2026-01-01-alpha"]

    def test_excludes_internal_files(self, client):
        """agent-log.jsonl and checkpoints.json are excluded from file lists."""
        c, output_dir = client
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

    def test_ignores_files_at_output_root(self, client):
        """Files directly in output/ (not in subdirs) are not listed as runs."""
        c, output_dir = client
        (output_dir / "agent-log.jsonl").write_text("{}", encoding="utf-8")
        _make_run(output_dir, "2026-03-01-real-run", {"post.md": "x"})

        resp = c.get("/api/runs")
        ids = [r["id"] for r in resp.get_json()["runs"]]
        assert ids == ["2026-03-01-real-run"]


class TestGetFile:
    """Tests for GET /api/runs/<run_id>/file."""

    def test_returns_file_content(self, client):
        """Returns the raw file content as text/plain."""
        c, output_dir = client
        _make_run(output_dir, "2026-04-01-test", {"post.md": "# My Post\n"})

        resp = c.get("/api/runs/2026-04-01-test/file?name=post.md")
        assert resp.status_code == 200
        assert resp.data.decode("utf-8") == "# My Post\n"
        assert "text/plain" in resp.content_type

    def test_404_for_missing_file(self, client):
        """Returns 404 when the file does not exist."""
        c, output_dir = client
        _make_run(output_dir, "2026-04-02-test", {})

        resp = c.get("/api/runs/2026-04-02-test/file?name=missing.md")
        assert resp.status_code == 404

    def test_400_for_missing_name_param(self, client):
        """Returns 400 when the name query parameter is absent."""
        c, output_dir = client
        _make_run(output_dir, "2026-04-03-test", {})

        resp = c.get("/api/runs/2026-04-03-test/file")
        assert resp.status_code == 400

    def test_path_traversal_rejected(self, client):
        """Path traversal in the name parameter is rejected with 403."""
        c, output_dir = client
        _make_run(output_dir, "2026-04-04-test", {})

        resp = c.get("/api/runs/2026-04-04-test/file?name=../../secrets.txt")
        assert resp.status_code == 403


class TestDownloadFile:
    """Tests for GET /api/runs/<run_id>/download/<filename>."""

    def test_serves_file_as_attachment(self, client):
        """Returns the file with Content-Disposition: attachment."""
        c, output_dir = client
        _make_run(output_dir, "2026-05-01-test", {"script.md": "# Script\n"})

        resp = c.get("/api/runs/2026-05-01-test/download/script.md")
        assert resp.status_code == 200
        assert "attachment" in resp.headers.get("Content-Disposition", "")
        assert "script.md" in resp.headers.get("Content-Disposition", "")

    def test_404_for_missing_file(self, client):
        """Returns 404 when the file does not exist."""
        c, output_dir = client
        _make_run(output_dir, "2026-05-02-test", {})

        resp = c.get("/api/runs/2026-05-02-test/download/missing.md")
        assert resp.status_code == 404

    def test_path_traversal_rejected(self, client):
        """Path traversal in the filename segment is rejected with 403."""
        c, output_dir = client
        _make_run(output_dir, "2026-05-03-test", {})

        resp = c.get("/api/runs/2026-05-03-test/download/../../secrets.txt")
        # Flask may normalise the URL before it reaches the view; either 403
        # or 404 is acceptable — the important thing is it does NOT serve the
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
            flask_app.config["TESTING"] = True

            with patch.object(app_module, "_OUTPUT_DIR", tmp_path):
                run_id = "prop-test-run"
                _make_run(tmp_path, run_id, {})

                with flask_app.test_client() as c:
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
