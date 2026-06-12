"""Verification tests for the legacy Flask Console after the wiring-factory refactor.

Covers Requirement 13 of the bullpen-console-go spec: the legacy Flask Console
(``scripts/gui/app.py``, ``run_gui.py``, ``log_tailer.py``, ``bullpen.bat``) must
remain operational after slice #34 collapsed the triplicated agent wiring into the
shared ``magic_content_engine.bullpen.wiring.build_agent_callables(...)`` factory,
and it must still use its ``threading.Event``-based approval gate.

This is a VERIFICATION slice. The tests prove the legacy path still works; they do
not rewrite it. Acceptance criteria exercised here:

  - 13.1 The legacy Flask Console boots and can run a pipeline (app imports, health
    endpoint responds, a pipeline run completes through the legacy wiring).
  - 13.2 After the Wiring_Factory refactor, the legacy GUI's
    ``_agent_factory.build_agent_callables`` delegates to the shared
    ``wiring.build_agent_callables`` and the ``threading.Event`` approval gate still
    resumes / rejects a real ``run_pipeline()`` execution.

All agent / model calls are stubbed, so the tests make no AWS or network calls and
run deterministically. The ``threading.Event`` gate is always unblocked from a
background actor (the Flask approve/reject endpoint) *after* the pipeline thread has
reached and is blocking on the gate — never pre-seeded — so there is no hang.

Requirements: REQ-bullpen-console-go-13 (Legacy Flask Console Retained in Parallel).
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import path setup — mirror test_bullpen_web_gui.py so ``scripts.gui.*`` and the
# legacy GUI's local ``import pipeline_runner`` fallback both resolve.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import scripts.gui.app as app_module  # noqa: E402
from scripts.gui import _agent_factory  # noqa: E402
from scripts.gui.app import RunState  # noqa: E402
from scripts.gui.pipeline_runner import _make_approval_fn  # noqa: E402

from magic_content_engine.bullpen import wiring  # noqa: E402
from magic_content_engine.bullpen.editor_in_chief import run_pipeline  # noqa: E402
from magic_content_engine.bullpen.models import (  # noqa: E402
    BullpenBrief,
    ContentBrief,
    FileEntry,
    ResearchBrief,
    ScoredArticle,
    SubeditorReview,
    Verdict,
    WriterManifest,
)

# Generous upper bound: the gate is always unblocked by the test, so a healthy
# run finishes in well under a second. This is only a backstop against a hang.
_JOIN_TIMEOUT = 10.0
_GATE_REACHED_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Deterministic stub agent callables — no AWS, no network, no model calls.
# ---------------------------------------------------------------------------

_PUBLISH_FILENAME = "post.md"


def _stub_researcher(brief: BullpenBrief) -> ResearchBrief:
    return ResearchBrief(
        articles=[
            ScoredArticle(
                title="Kiro IDE changelog",
                url="https://kiro.dev/changelog/ide/",
                source="kiro.dev",
                relevance_score=5,
                summary="A new Kiro IDE release.",
            )
        ],
        sources_crawled=["kiro.dev"],
        sources_failed=[],
        run_timestamp="2025-01-15T00:00:00+00:00",
    )


def _stub_desk_editor(research_brief, topic, requested_outputs) -> ContentBrief:
    return ContentBrief(
        selected_articles=list(research_brief.articles),
        editorial_angle="Aotearoa builder angle",
        tone_guidance="conversational",
        output_types=list(requested_outputs),
        run_timestamp="2025-01-15T00:00:00+00:00",
    )


def _stub_writer(content_brief, revision_feedback) -> WriterManifest:
    return WriterManifest(
        files_written=[FileEntry(path=_PUBLISH_FILENAME, output_type="blog", word_count=500)],
        voice_rules_applied=True,
        run_timestamp="2025-01-15T00:00:00+00:00",
    )


def _stub_subeditor(manifest) -> SubeditorReview:
    # Single "publish" verdict -> files_published is non-empty -> the approval
    # gate IS presented (Requirement 3.1), which is exactly what we want to test.
    return SubeditorReview(
        verdicts=[Verdict(filename=_PUBLISH_FILENAME, verdict="publish", feedback="")],
        run_timestamp="2025-01-15T00:00:00+00:00",
    )


def _make_stub_callables(publisher_spy):
    """Return the 7 agent callables run_pipeline needs (approval_fn supplied separately)."""
    return {
        "researcher_fn": _stub_researcher,
        "desk_editor_fn": _stub_desk_editor,
        "writer_fn": _stub_writer,
        "subeditor_fn": _stub_subeditor,
        "publisher_fn": publisher_spy,
        "log_fn": lambda event: None,
        "checkpoint_fn": lambda cp: None,
    }


def _brief() -> BullpenBrief:
    return BullpenBrief(
        topic="Kiro IDE 1.0 launch",
        requested_outputs=["blog"],
        run_date=date(2025, 1, 15),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_run_state():
    app_module._run_state = RunState()
    yield
    app_module._run_state = RunState()


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# 13.1 — Legacy Flask Console boots
# ---------------------------------------------------------------------------


class TestLegacyConsoleBoots:
    def test_flask_app_object_exists(self):
        from flask import Flask

        assert isinstance(app_module.app, Flask)

    def test_health_endpoint_responds_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "ok"}

    def test_run_gui_entry_point_imports(self):
        # bullpen.bat -> scripts/run_gui.py must still import after the refactor.
        from scripts import run_gui

        assert hasattr(run_gui, "main")


# ---------------------------------------------------------------------------
# 13.2 — Legacy GUI factory delegates to the shared wiring factory
# ---------------------------------------------------------------------------


class TestLegacyFactoryUsesSharedWiring:
    def test_factory_delegates_to_shared_build_agent_callables(self):
        """The GUI factory must build via wiring.build_agent_callables (slice #34)."""
        run_state = RunState(output_dir="output/run-xyz")
        sentinel = {
            "researcher_fn": lambda b: None,
            "desk_editor_fn": lambda *a: None,
            "writer_fn": lambda *a: None,
            "subeditor_fn": lambda m: None,
            "publisher_fn": lambda f: None,
            "approval_fn": lambda r: True,
            "log_fn": lambda e: None,
            "checkpoint_fn": lambda c: None,
        }
        with patch.object(_agent_factory, "_build_shared", return_value=dict(sentinel)) as spy:
            result = _agent_factory.build_agent_callables(run_state, _brief())

        spy.assert_called_once()
        _, kwargs = spy.call_args
        # The GUI supplies output_dir from run_state and its own approval_fn.
        assert kwargs["output_dir"] == "output/run-xyz"
        assert callable(kwargs["approval_fn"])
        # Historical GUI contract: approval_fn is stripped (the event gate is
        # supplied separately by pipeline_runner).
        assert "approval_fn" not in result

    def test_factory_returns_seven_callables_without_approval_fn(self):
        """With AWS clients stubbed, the factory returns the 7 non-gate callables."""
        run_state = RunState(output_dir="output/run-abc")
        with patch.object(wiring, "boto3", MagicMock()), \
             patch.object(wiring, "_make_bedrock_scorer", return_value=lambda **k: ""), \
             patch.object(wiring, "_make_bedrock_llm", return_value=lambda **k: ""):
            callables = _agent_factory.build_agent_callables(run_state, _brief())

        assert set(callables) == {
            "researcher_fn",
            "desk_editor_fn",
            "writer_fn",
            "subeditor_fn",
            "publisher_fn",
            "log_fn",
            "checkpoint_fn",
        }
        for name, fn in callables.items():
            assert callable(fn), f"{name} should be callable"


# ---------------------------------------------------------------------------
# 13.2 — threading.Event approval gate still drives a real pipeline run
# ---------------------------------------------------------------------------


def _run_pipeline_in_thread(run_state, publisher_spy):
    """Start run_pipeline in a daemon thread using the legacy event gate.

    Returns (thread, result_box). The pipeline result lands in result_box[0].
    """
    approval_fn = _make_approval_fn(run_state)
    callables = _make_stub_callables(publisher_spy)
    result_box: list = []

    def _target():
        result_box.append(
            run_pipeline(brief=_brief(), approval_fn=approval_fn, **callables)
        )

    t = threading.Thread(target=_target, daemon=True, name="legacy-pipeline")
    t.start()
    return t, result_box


def _wait_for_gate(run_state) -> None:
    """Block until the pipeline thread has reached and is waiting on the gate."""
    deadline = time.time() + _GATE_REACHED_TIMEOUT
    while time.time() < deadline:
        if run_state.approval_event is not None:
            return
        time.sleep(0.01)
    raise AssertionError("pipeline never reached the approval gate")


class TestLegacyEventApprovalGate:
    def test_approve_via_endpoint_resumes_pipeline_and_publishes(self, client):
        run_state = app_module._run_state
        publisher_spy = MagicMock()

        thread, result_box = _run_pipeline_in_thread(run_state, publisher_spy)
        _wait_for_gate(run_state)

        # Gate is blocking: the pipeline has NOT published yet and is still alive.
        assert thread.is_alive()
        publisher_spy.assert_not_called()

        # The Flask approve endpoint sets the threading.Event from "outside".
        resp = client.post("/api/run/approve")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "approved"

        thread.join(timeout=_JOIN_TIMEOUT)
        assert not thread.is_alive(), "pipeline did not resume after Event was set"

        result = result_box[0]
        assert result.status == "success"
        assert result.files_published == [_PUBLISH_FILENAME]
        publisher_spy.assert_called_once_with([_PUBLISH_FILENAME])

    def test_reject_via_endpoint_resumes_pipeline_without_publishing(self, client):
        run_state = app_module._run_state
        publisher_spy = MagicMock()

        thread, result_box = _run_pipeline_in_thread(run_state, publisher_spy)
        _wait_for_gate(run_state)

        assert thread.is_alive()

        resp = client.post("/api/run/reject")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "rejected"

        thread.join(timeout=_JOIN_TIMEOUT)
        assert not thread.is_alive(), "pipeline did not resume after Event was set"

        result = result_box[0]
        assert result.status == "success"
        assert result.files_published == []
        publisher_spy.assert_not_called()

    def test_gate_blocks_until_event_set(self):
        """Directly assert the gate blocks: it must not return before the Event is set."""
        run_state = RunState()
        approval_fn = _make_approval_fn(run_state)
        returned: list = []

        def _call_gate():
            returned.append(approval_fn(None))

        gate_thread = threading.Thread(target=_call_gate, daemon=True)
        gate_thread.start()

        # Wait until the gate has registered its Event, then confirm it is still
        # blocked (has not returned) before we set the Event.
        _wait_for_gate(run_state)
        assert returned == [], "gate returned before the Event was set"
        assert gate_thread.is_alive()

        # Unblock from this (background) actor, after polling began.
        run_state.approval_result = True
        run_state.approval_event.set()

        gate_thread.join(timeout=_JOIN_TIMEOUT)
        assert not gate_thread.is_alive()
        assert returned == [True]
