"""Unit tests for the shared agent wiring factory.

Covers Requirement 9 of the bullpen-console-go spec: the triplicated agent
wiring is collapsed into one ``build_agent_callables(...)`` factory that
returns every callable ``run_pipeline()`` needs, parameterised by the
``approval_fn`` each entry point supplies.

These tests construct the AWS-backed callables but never invoke them, so no
real AWS credentials or network access are required (boto3 clients resolve
credentials lazily on first call).
"""

from __future__ import annotations

from datetime import date

import pytest

from magic_content_engine.bullpen.models import BullpenBrief, SubeditorReview

# Module under test — import deferred so a missing module produces a clear
# collection error rather than an obscure failure elsewhere.
from magic_content_engine.bullpen import wiring


EXPECTED_KEYS = {
    "researcher_fn",
    "desk_editor_fn",
    "writer_fn",
    "subeditor_fn",
    "publisher_fn",
    "approval_fn",
    "log_fn",
    "checkpoint_fn",
}


def _brief() -> BullpenBrief:
    return BullpenBrief(
        topic="Kiro IDE 1.0 launch",
        requested_outputs=["blog", "youtube"],
        run_date=date(2025, 1, 15),
    )


def _approval_stub(review: SubeditorReview | None) -> bool:
    return True


def test_returns_all_eight_expected_keys(tmp_path):
    callables = wiring.build_agent_callables(
        brief=_brief(),
        output_dir=str(tmp_path),
        approval_fn=_approval_stub,
        dry_run=True,
    )
    assert set(callables.keys()) == EXPECTED_KEYS


def test_passed_in_approval_fn_is_returned(tmp_path):
    callables = wiring.build_agent_callables(
        brief=_brief(),
        output_dir=str(tmp_path),
        approval_fn=_approval_stub,
        dry_run=True,
    )
    # The factory must not wrap or replace the gate the caller supplies.
    assert callables["approval_fn"] is _approval_stub


def test_all_returned_values_are_callable(tmp_path):
    callables = wiring.build_agent_callables(
        brief=_brief(),
        output_dir=str(tmp_path),
        approval_fn=_approval_stub,
        dry_run=True,
    )
    for key in EXPECTED_KEYS:
        assert callable(callables[key]), f"{key} should be callable"


def test_keyword_only_signature(tmp_path):
    # All parameters are keyword-only; positional invocation must fail.
    with pytest.raises(TypeError):
        wiring.build_agent_callables(_brief(), str(tmp_path), _approval_stub)  # type: ignore[misc]


def test_dry_run_researcher_returns_stub_research_brief(tmp_path):
    callables = wiring.build_agent_callables(
        brief=_brief(),
        output_dir=str(tmp_path),
        approval_fn=_approval_stub,
        dry_run=True,
    )
    brief = _brief()
    research_brief = callables["researcher_fn"](brief)
    # The dry-run stub must not crawl; it returns a minimal ResearchBrief.
    assert research_brief.articles
    assert research_brief.sources_failed == []
