"""Tests for the Editor-in-Chief pipeline orchestrator.

Covers:
- BullpenBrief validation (empty topic rejected, no outputs rejected)
- Pipeline sequence enforced (agents called in order)
- Revision loop bounded at 2 cycles
- Spike verdict discards without re-spawn
- Researcher failure halts pipeline
- Desk Editor failure halts pipeline
- Writer partial failure continues remaining types
- Property-based tests: BullpenBrief validation, pipeline sequence, revision loop bound

Requirements: Bullpen REQ-1, REQ-2, REQ-11, REQ-22, REQ-25
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from magic_content_engine.bullpen.models import (
    AMILogEvent,
    BullpenBrief,
    Checkpoint,
    ContentBrief,
    FileEntry,
    ResearchBrief,
    ScoredArticle,
    SubeditorReview,
    Verdict,
    WriterManifest,
)
from magic_content_engine.bullpen.editor_in_chief import (
    MAX_REVISION_CYCLES,
    VALID_OUTPUT_TYPES,
    BullpenBriefValidationError,
    PipelineResult,
    run_pipeline,
    validate_brief,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_brief(
    topic: str = "Kiro IDE and AgentCore on AWS",
    requested_outputs: list | None = None,
) -> BullpenBrief:
    if requested_outputs is None:
        requested_outputs = ["blog"]
    return BullpenBrief(topic=topic, requested_outputs=requested_outputs)


def _make_research_brief() -> ResearchBrief:
    return ResearchBrief(
        articles=[
            ScoredArticle(
                title="Kiro IDE 1.0",
                url="https://kiro.dev/changelog/ide/",
                source="kiro.dev",
                relevance_score=5,
                summary="Kiro IDE 1.0 released.",
            )
        ],
        sources_crawled=["kiro.dev"],
        sources_failed=[],
        run_timestamp="2025-07-14T09:00:00+00:00",
    )


def _make_content_brief(output_types: list | None = None) -> ContentBrief:
    if output_types is None:
        output_types = ["blog"]
    return ContentBrief(
        selected_articles=[
            ScoredArticle(
                title="Kiro IDE 1.0",
                url="https://kiro.dev/changelog/ide/",
                source="kiro.dev",
                relevance_score=5,
                summary="Kiro IDE 1.0 released.",
            )
        ],
        editorial_angle="Kiro IDE changes the game for Aotearoa builders.",
        tone_guidance="Conversational, no em-dashes, short sentences.",
        output_types=output_types,
        run_timestamp="2025-07-14T09:00:00+00:00",
    )


def _make_manifest(filenames: list | None = None) -> WriterManifest:
    if filenames is None:
        filenames = ["post.md"]
    return WriterManifest(
        files_written=[
            FileEntry(path=f, output_type="blog", word_count=800)
            for f in filenames
        ],
        voice_rules_applied=True,
        run_timestamp="2025-07-14T09:00:00+00:00",
    )


def _make_review(verdicts: list | None = None) -> SubeditorReview:
    if verdicts is None:
        verdicts = [Verdict(filename="post.md", verdict="publish", feedback="")]
    return SubeditorReview(verdicts=verdicts, run_timestamp="2025-07-14T09:00:00+00:00")


def _noop_log(event: AMILogEvent) -> None:
    pass


def _noop_checkpoint(cp: Checkpoint) -> None:
    pass


def _make_pipeline_fns(
    researcher_result=None,
    desk_editor_result=None,
    writer_result=None,
    subeditor_result=None,
    publisher_result=None,
    approval_result: bool = True,
    researcher_raises=None,
    desk_editor_raises=None,
    writer_raises=None,
    subeditor_raises=None,
):
    """Build a set of pipeline callables for testing."""
    call_log: list[str] = []

    def researcher_fn(brief):
        call_log.append("researcher")
        if researcher_raises:
            raise researcher_raises
        return researcher_result or _make_research_brief()

    def desk_editor_fn(research_brief, topic, output_types):
        call_log.append("desk_editor")
        if desk_editor_raises:
            raise desk_editor_raises
        return desk_editor_result or _make_content_brief(output_types)

    def writer_fn(content_brief, revision_feedback):
        call_log.append(f"writer(feedback={'yes' if revision_feedback else 'no'})")
        if writer_raises:
            raise writer_raises
        return writer_result or _make_manifest()

    def subeditor_fn(manifest):
        call_log.append("subeditor")
        if subeditor_raises:
            raise subeditor_raises
        return subeditor_result or _make_review()

    def publisher_fn(approved_files):
        call_log.append("publisher")
        return publisher_result

    def approval_fn(review):
        call_log.append("approval")
        return approval_result

    return (
        researcher_fn,
        desk_editor_fn,
        writer_fn,
        subeditor_fn,
        publisher_fn,
        approval_fn,
        call_log,
    )


# ---------------------------------------------------------------------------
# 1. BullpenBrief validation
# ---------------------------------------------------------------------------


class TestBullpenBriefValidation:
    def test_valid_brief_passes(self):
        brief = _make_brief()
        validate_brief(brief)  # should not raise

    def test_empty_topic_rejected(self):
        brief = BullpenBrief(topic="", requested_outputs=["blog"])
        with pytest.raises(BullpenBriefValidationError, match="topic"):
            validate_brief(brief)

    def test_whitespace_only_topic_rejected(self):
        brief = BullpenBrief(topic="   ", requested_outputs=["blog"])
        with pytest.raises(BullpenBriefValidationError, match="topic"):
            validate_brief(brief)

    def test_no_outputs_rejected(self):
        brief = BullpenBrief(topic="Kiro IDE", requested_outputs=[])
        with pytest.raises(BullpenBriefValidationError, match="requested_outputs"):
            validate_brief(brief)

    def test_all_invalid_output_types_rejected(self):
        brief = BullpenBrief(topic="Kiro IDE", requested_outputs=["invalid_type", "also_bad"])
        with pytest.raises(BullpenBriefValidationError, match="no valid output types"):
            validate_brief(brief)

    def test_mixed_valid_invalid_outputs_passes(self):
        brief = BullpenBrief(topic="Kiro IDE", requested_outputs=["blog", "invalid_type"])
        validate_brief(brief)  # should not raise — at least one valid type

    def test_all_valid_output_types_accepted(self):
        for output_type in VALID_OUTPUT_TYPES:
            brief = BullpenBrief(topic="Kiro IDE", requested_outputs=[output_type])
            validate_brief(brief)  # should not raise

    def test_pipeline_returns_error_on_invalid_brief(self):
        brief = BullpenBrief(topic="", requested_outputs=["blog"])
        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns()

        result = run_pipeline(
            brief=brief,
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        assert result.status == "error"
        assert len(result.errors) == 1
        assert result.errors[0]["step"] == "validation"
        # No agents should have been called
        assert call_log == []


# ---------------------------------------------------------------------------
# 2. Pipeline sequence enforced
# ---------------------------------------------------------------------------


class TestPipelineSequence:
    def test_happy_path_calls_agents_in_order(self):
        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns()

        result = run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        assert result.status == "success"
        # Verify strict ordering
        assert call_log[0] == "researcher"
        assert call_log[1] == "desk_editor"
        assert call_log[2] == "writer(feedback=no)"
        assert call_log[3] == "subeditor"
        assert call_log[4] == "approval"
        assert call_log[5] == "publisher"

    def test_researcher_called_before_desk_editor(self):
        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns()

        run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        researcher_idx = next(i for i, c in enumerate(call_log) if c == "researcher")
        desk_editor_idx = next(i for i, c in enumerate(call_log) if c == "desk_editor")
        assert researcher_idx < desk_editor_idx

    def test_writer_called_before_subeditor(self):
        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns()

        run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        writer_idx = next(i for i, c in enumerate(call_log) if "writer" in c)
        subeditor_idx = next(i for i, c in enumerate(call_log) if c == "subeditor")
        assert writer_idx < subeditor_idx

    def test_publisher_called_after_approval(self):
        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns()

        run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        approval_idx = next(i for i, c in enumerate(call_log) if c == "approval")
        publisher_idx = next(i for i, c in enumerate(call_log) if c == "publisher")
        assert approval_idx < publisher_idx

    def test_publisher_not_called_when_approval_rejected(self):
        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns(approval_result=False)

        result = run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        assert "publisher" not in call_log
        assert result.files_published == []

    def test_result_contains_published_files(self):
        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns()

        result = run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        assert "post.md" in result.files_published

    def test_checkpoints_saved_after_each_agent(self):
        checkpoints: list[Checkpoint] = []

        def _capture_checkpoint(cp: Checkpoint) -> None:
            checkpoints.append(cp)

        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns()

        run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_capture_checkpoint,
        )

        agent_types = [cp.agent_type for cp in checkpoints]
        assert "researcher" in agent_types
        assert "desk_editor" in agent_types
        assert "writer" in agent_types
        assert "subeditor" in agent_types
        assert "publisher" in agent_types

    def test_log_events_emitted_for_each_agent(self):
        events: list[AMILogEvent] = []

        def _capture_log(event: AMILogEvent) -> None:
            events.append(event)

        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns()

        run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_capture_log,
            checkpoint_fn=_noop_checkpoint,
        )

        event_types = [e.event_type for e in events]
        assert "brief_accepted" in event_types
        assert "agent_invoked" in event_types
        assert "agent_completed" in event_types


# ---------------------------------------------------------------------------
# 2b. Researcher completion event carries crawl/score counts — Issue #59
# ---------------------------------------------------------------------------


class TestResearcherCompletionEventCounts:
    """The researcher ``agent_completed`` event must carry the KPI counts.

    Issue #59: the Console dashboard KPI tiles render the raw crawled count,
    the scored-above-threshold (kept) count, and the source counts. These must
    appear in the researcher completion event ``details``.
    """

    def _make_research_brief_with_counts(self) -> ResearchBrief:
        # 2 scored (kept) articles, 5 crawled raw, 3 sources crawled, 1 failed.
        return ResearchBrief(
            articles=[
                ScoredArticle(
                    title="Kiro IDE 1.0",
                    url="https://kiro.dev/changelog/ide/",
                    source="kiro.dev",
                    relevance_score=5,
                    summary="Kiro IDE 1.0 released.",
                ),
                ScoredArticle(
                    title="AgentCore GA",
                    url="https://aws.amazon.com/new/agentcore",
                    source="aws.amazon.com/new/",
                    relevance_score=4,
                    summary="AgentCore GA.",
                ),
            ],
            sources_crawled=["kiro.dev", "aws.amazon.com/new/", "community.aws"],
            sources_failed=["strandsagents.com"],
            run_timestamp="2025-07-14T09:00:00+00:00",
            articles_crawled=5,
        )

    def _run_and_capture(self) -> list[AMILogEvent]:
        events: list[AMILogEvent] = []
        rb = self._make_research_brief_with_counts()
        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns(
            researcher_result=rb
        )

        run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=lambda e: events.append(e),
            checkpoint_fn=_noop_checkpoint,
        )
        return events

    def _researcher_completed_details(self) -> dict:
        events = self._run_and_capture()
        completed = [
            e for e in events
            if e.event_type == "agent_completed" and e.agent_type == "researcher"
        ]
        assert len(completed) == 1
        return completed[0].details

    def test_event_includes_articles_crawled(self):
        details = self._researcher_completed_details()
        assert details["articles_crawled"] == 5

    def test_event_includes_scored_above_threshold(self):
        details = self._researcher_completed_details()
        assert details["scored_above_threshold"] == 2

    def test_event_includes_sources_crawled_count(self):
        details = self._researcher_completed_details()
        assert details["sources_crawled"] == 3

    def test_event_includes_sources_failed_count(self):
        details = self._researcher_completed_details()
        assert details["sources_failed"] == 1

    def test_event_preserves_articles_count_key(self):
        """The existing articles_count key (kept count) must not regress."""
        details = self._researcher_completed_details()
        assert details["articles_count"] == 2


# ---------------------------------------------------------------------------
# 3. Revision loop bounded at 2 cycles
# ---------------------------------------------------------------------------


class TestRevisionLoop:
    def test_revise_verdict_triggers_writer_reinvocation(self):
        """When Subeditor returns revise, Writer is called again with feedback."""
        call_log: list[str] = []
        subeditor_calls = [0]

        def researcher_fn(brief):
            return _make_research_brief()

        def desk_editor_fn(rb, topic, output_types):
            return _make_content_brief(output_types)

        def writer_fn(content_brief, revision_feedback):
            call_log.append(f"writer(feedback={'yes' if revision_feedback else 'no'})")
            return _make_manifest()

        def subeditor_fn(manifest):
            subeditor_calls[0] += 1
            if subeditor_calls[0] == 1:
                # First call: revise
                return _make_review([
                    Verdict(filename="post.md", verdict="revise", feedback="Fix the em-dash.")
                ])
            # Second call: publish
            return _make_review([
                Verdict(filename="post.md", verdict="publish", feedback="")
            ])

        def publisher_fn(files):
            call_log.append("publisher")

        def approval_fn(review):
            return True

        result = run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        # Writer called twice: initial + 1 revision
        writer_calls = [c for c in call_log if "writer" in c]
        assert len(writer_calls) == 2
        assert writer_calls[0] == "writer(feedback=no)"
        assert writer_calls[1] == "writer(feedback=yes)"
        assert result.status == "success"
        assert "post.md" in result.files_published

    def test_revision_loop_bounded_at_max_cycles(self):
        """After MAX_REVISION_CYCLES revisions, file is escalated not re-spawned."""
        writer_call_count = [0]
        subeditor_call_count = [0]

        def researcher_fn(brief):
            return _make_research_brief()

        def desk_editor_fn(rb, topic, output_types):
            return _make_content_brief(output_types)

        def writer_fn(content_brief, revision_feedback):
            writer_call_count[0] += 1
            return _make_manifest()

        def subeditor_fn(manifest):
            subeditor_call_count[0] += 1
            # Always return revise
            return _make_review([
                Verdict(filename="post.md", verdict="revise", feedback="Still needs work.")
            ])

        def publisher_fn(files):
            pass

        def approval_fn(review):
            return True

        result = run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        # Writer called at most MAX_REVISION_CYCLES + 1 times (initial + revisions)
        assert writer_call_count[0] <= MAX_REVISION_CYCLES + 1
        # File should be escalated, not published
        assert "post.md" in result.files_escalated
        assert "post.md" not in result.files_published

    def test_spike_verdict_discards_without_writer_reinvocation(self):
        """Spike verdict: file discarded, Writer NOT re-invoked for that file."""
        writer_call_count = [0]

        def researcher_fn(brief):
            return _make_research_brief()

        def desk_editor_fn(rb, topic, output_types):
            return _make_content_brief(output_types)

        def writer_fn(content_brief, revision_feedback):
            writer_call_count[0] += 1
            return _make_manifest()

        def subeditor_fn(manifest):
            return _make_review([
                Verdict(filename="post.md", verdict="spike", feedback="Off-topic content.")
            ])

        def publisher_fn(files):
            pass

        def approval_fn(review):
            return True

        result = run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        # Writer called exactly once (initial pass only)
        assert writer_call_count[0] == 1
        # Spiked file not published, not escalated
        assert "post.md" not in result.files_published
        assert "post.md" not in result.files_escalated

    def test_spike_rationale_logged(self):
        """Spike rationale is logged via log_fn."""
        log_events: list[AMILogEvent] = []

        def researcher_fn(brief):
            return _make_research_brief()

        def desk_editor_fn(rb, topic, output_types):
            return _make_content_brief(output_types)

        def writer_fn(content_brief, revision_feedback):
            return _make_manifest()

        def subeditor_fn(manifest):
            return _make_review([
                Verdict(filename="post.md", verdict="spike", feedback="Off-topic content.")
            ])

        def publisher_fn(files):
            pass

        def approval_fn(review):
            return True

        run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=lambda e: log_events.append(e),
            checkpoint_fn=_noop_checkpoint,
        )

        spike_events = [e for e in log_events if e.event_type == "file_spiked"]
        assert len(spike_events) == 1
        assert spike_events[0].details["filename"] == "post.md"
        assert "Off-topic" in spike_events[0].details["rationale"]

    def test_max_revisions_escalation_logged(self):
        """When max revisions reached, escalation is logged."""
        log_events: list[AMILogEvent] = []

        def researcher_fn(brief):
            return _make_research_brief()

        def desk_editor_fn(rb, topic, output_types):
            return _make_content_brief(output_types)

        def writer_fn(content_brief, revision_feedback):
            return _make_manifest()

        def subeditor_fn(manifest):
            return _make_review([
                Verdict(filename="post.md", verdict="revise", feedback="Still needs work.")
            ])

        def publisher_fn(files):
            pass

        def approval_fn(review):
            return True

        run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=lambda e: log_events.append(e),
            checkpoint_fn=_noop_checkpoint,
        )

        escalation_events = [e for e in log_events if e.event_type == "file_escalated"]
        assert len(escalation_events) >= 1
        assert any(
            e.details.get("reason") == "max_revisions_reached"
            for e in escalation_events
        )


# ---------------------------------------------------------------------------
# 4. Researcher failure halts pipeline
# ---------------------------------------------------------------------------


class TestResearcherFailure:
    def test_researcher_failure_halts_pipeline(self):
        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns(
            researcher_raises=RuntimeError("Researcher timed out")
        )

        result = run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        assert result.status == "halted"
        assert len(result.errors) == 1
        assert result.errors[0]["step"] == "researcher"
        # Downstream agents must not be called
        assert "desk_editor" not in call_log
        assert "writer(feedback=no)" not in call_log
        assert "subeditor" not in call_log
        assert "publisher" not in call_log

    def test_researcher_failure_logs_error(self):
        log_events: list[AMILogEvent] = []

        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns(
            researcher_raises=RuntimeError("Network error")
        )

        run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=lambda e: log_events.append(e),
            checkpoint_fn=_noop_checkpoint,
        )

        error_events = [e for e in log_events if e.event_type == "agent_error"]
        assert len(error_events) == 1
        assert error_events[0].agent_type == "researcher"

    def test_researcher_failure_saves_failure_checkpoint(self):
        checkpoints: list[Checkpoint] = []

        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns(
            researcher_raises=RuntimeError("Timeout")
        )

        run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=lambda cp: checkpoints.append(cp),
        )

        failure_cps = [cp for cp in checkpoints if cp.status == "failure"]
        assert len(failure_cps) == 1
        assert failure_cps[0].agent_type == "researcher"


# ---------------------------------------------------------------------------
# 5. Desk Editor failure halts pipeline
# ---------------------------------------------------------------------------


class TestDeskEditorFailure:
    def test_desk_editor_failure_halts_pipeline(self):
        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns(
            desk_editor_raises=RuntimeError("Desk editor failed")
        )

        result = run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        assert result.status == "halted"
        assert len(result.errors) == 1
        assert result.errors[0]["step"] == "desk_editor"
        # Researcher was called, but downstream agents must not be
        assert "researcher" in call_log
        assert "writer(feedback=no)" not in call_log
        assert "subeditor" not in call_log
        assert "publisher" not in call_log

    def test_desk_editor_failure_logs_error(self):
        log_events: list[AMILogEvent] = []

        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns(
            desk_editor_raises=ValueError("Bad content brief")
        )

        run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=lambda e: log_events.append(e),
            checkpoint_fn=_noop_checkpoint,
        )

        error_events = [e for e in log_events if e.event_type == "agent_error"]
        assert any(e.agent_type == "desk_editor" for e in error_events)


# ---------------------------------------------------------------------------
# 6. Writer partial failure continues remaining types
# ---------------------------------------------------------------------------


class TestWriterPartialFailure:
    def test_writer_partial_failure_continues_remaining_types(self):
        """Writer raises for one output type but returns manifest for others."""
        # The writer_fn in the pipeline receives the full content_brief.
        # Partial failure is handled inside writer_fn itself (log-and-continue).
        # We simulate this by having writer_fn return a partial manifest.
        partial_manifest = WriterManifest(
            files_written=[
                FileEntry(path="post.md", output_type="blog", word_count=800),
                # script.md is missing — writer failed for youtube
            ],
            voice_rules_applied=True,
            run_timestamp="2025-07-14T09:00:00+00:00",
        )

        def researcher_fn(brief):
            return _make_research_brief()

        def desk_editor_fn(rb, topic, output_types):
            return _make_content_brief(["blog", "youtube"])

        def writer_fn(content_brief, revision_feedback):
            # Returns partial manifest (youtube failed internally)
            return partial_manifest

        def subeditor_fn(manifest):
            # Only post.md in manifest
            return _make_review([
                Verdict(filename="post.md", verdict="publish", feedback="")
            ])

        publisher_calls: list[list] = []

        def publisher_fn(files):
            publisher_calls.append(files)

        def approval_fn(review):
            return True

        result = run_pipeline(
            brief=_make_brief(requested_outputs=["blog", "youtube"]),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        # Pipeline continues and publishes the successful file
        assert "post.md" in result.files_published
        assert len(publisher_calls) == 1


# ---------------------------------------------------------------------------
# 7. Subeditor failure escalates all pending files
# ---------------------------------------------------------------------------


class TestSubeditorFailure:
    def test_subeditor_failure_escalates_all_pending_files(self):
        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns(
            subeditor_raises=RuntimeError("Subeditor crashed")
        )

        result = run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        assert "post.md" in result.files_escalated
        assert "post.md" not in result.files_published

    def test_subeditor_failure_logs_error(self):
        log_events: list[AMILogEvent] = []

        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns(
            subeditor_raises=RuntimeError("Subeditor crashed")
        )

        run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=lambda e: log_events.append(e),
            checkpoint_fn=_noop_checkpoint,
        )

        error_events = [e for e in log_events if e.event_type == "agent_error"]
        assert any(e.agent_type == "subeditor" for e in error_events)

        escalation_events = [e for e in log_events if e.event_type == "file_escalated"]
        assert len(escalation_events) >= 1


# ---------------------------------------------------------------------------
# 8. PipelineResult dataclass
# ---------------------------------------------------------------------------


class TestPipelineResult:
    def test_pipeline_result_has_required_fields(self):
        result = PipelineResult(
            status="success",
            files_published=["post.md"],
            files_escalated=[],
            errors=[],
            run_timestamp="2025-07-14T09:00:00+00:00",
        )
        assert result.status == "success"
        assert result.files_published == ["post.md"]
        assert result.files_escalated == []
        assert result.errors == []
        assert result.run_timestamp

    def test_pipeline_result_to_dict(self):
        result = PipelineResult(
            status="success",
            files_published=["post.md"],
            files_escalated=[],
            errors=[],
            run_timestamp="2025-07-14T09:00:00+00:00",
        )
        d = result.to_dict()
        assert d["status"] == "success"
        assert d["files_published"] == ["post.md"]
        assert "run_timestamp" in d

    def test_run_timestamp_present_in_result(self):
        (researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
         publisher_fn, approval_fn, call_log) = _make_pipeline_fns()

        result = run_pipeline(
            brief=_make_brief(),
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        assert result.run_timestamp
        # Should be a non-empty ISO 8601 string
        assert "T" in result.run_timestamp


# ---------------------------------------------------------------------------
# 9. Property-based tests (Hypothesis)
# ---------------------------------------------------------------------------

# Strategy for valid output types
_valid_output_type_strategy = st.sampled_from(sorted(VALID_OUTPUT_TYPES))

# Strategy for invalid output types (strings not in VALID_OUTPUT_TYPES)
_invalid_output_type_strategy = st.text(min_size=1, max_size=30).filter(
    lambda s: s not in VALID_OUTPUT_TYPES and s.strip()
)

# Strategy for non-empty topic strings
_topic_strategy = st.text(min_size=1, max_size=200).filter(str.strip)

# Strategy for empty/whitespace topics
_empty_topic_strategy = st.one_of(
    st.just(""),
    st.text(alphabet=" \t\n", min_size=1, max_size=20),
)


class TestPropertyBased:
    @given(topic=_topic_strategy, output_types=st.lists(_valid_output_type_strategy, min_size=1, max_size=5))
    @settings(max_examples=100)
    def test_valid_brief_always_passes_validation(self, topic: str, output_types: list):
        brief = BullpenBrief(topic=topic, requested_outputs=output_types)
        validate_brief(brief)  # must not raise

    @given(topic=_empty_topic_strategy)
    @settings(max_examples=50)
    def test_empty_topic_always_rejected(self, topic: str):
        brief = BullpenBrief(topic=topic, requested_outputs=["blog"])
        with pytest.raises(BullpenBriefValidationError):
            validate_brief(brief)

    @given(output_types=st.lists(_invalid_output_type_strategy, min_size=1, max_size=5))
    @settings(max_examples=50)
    def test_all_invalid_output_types_always_rejected(self, output_types: list):
        brief = BullpenBrief(topic="Kiro IDE", requested_outputs=output_types)
        with pytest.raises(BullpenBriefValidationError):
            validate_brief(brief)

    @given(
        topic=_topic_strategy,
        output_types=st.lists(_valid_output_type_strategy, min_size=1, max_size=3),
    )
    @settings(max_examples=50)
    def test_pipeline_sequence_invariant(self, topic: str, output_types: list):
        """Property: agents are always called in the correct order."""
        call_log: list[str] = []

        def researcher_fn(brief):
            call_log.append("researcher")
            return _make_research_brief()

        def desk_editor_fn(rb, t, ot):
            call_log.append("desk_editor")
            return _make_content_brief(ot)

        def writer_fn(cb, feedback):
            call_log.append("writer")
            return _make_manifest()

        def subeditor_fn(manifest):
            call_log.append("subeditor")
            return _make_review()

        def publisher_fn(files):
            call_log.append("publisher")

        def approval_fn(review):
            return True

        brief = BullpenBrief(topic=topic, requested_outputs=output_types)
        run_pipeline(
            brief=brief,
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        # Verify ordering invariant
        assert call_log.index("researcher") < call_log.index("desk_editor")
        assert call_log.index("desk_editor") < call_log.index("writer")
        assert call_log.index("writer") < call_log.index("subeditor")
        assert call_log.index("subeditor") < call_log.index("publisher")

    @given(
        topic=_topic_strategy,
        output_types=st.lists(_valid_output_type_strategy, min_size=1, max_size=2),
    )
    @settings(max_examples=50)
    def test_revision_loop_bounded_property(self, topic: str, output_types: list):
        """Property: Writer is called at most MAX_REVISION_CYCLES + 1 times."""
        writer_call_count = [0]

        def researcher_fn(brief):
            return _make_research_brief()

        def desk_editor_fn(rb, t, ot):
            return _make_content_brief(ot)

        def writer_fn(cb, feedback):
            writer_call_count[0] += 1
            return _make_manifest()

        def subeditor_fn(manifest):
            # Always revise to stress-test the bound
            return _make_review([
                Verdict(filename="post.md", verdict="revise", feedback="Needs work.")
            ])

        def publisher_fn(files):
            pass

        def approval_fn(review):
            return True

        brief = BullpenBrief(topic=topic, requested_outputs=output_types)
        run_pipeline(
            brief=brief,
            researcher_fn=researcher_fn,
            desk_editor_fn=desk_editor_fn,
            writer_fn=writer_fn,
            subeditor_fn=subeditor_fn,
            publisher_fn=publisher_fn,
            approval_fn=approval_fn,
            log_fn=_noop_log,
            checkpoint_fn=_noop_checkpoint,
        )

        assert writer_call_count[0] <= MAX_REVISION_CYCLES + 1, (
            f"Writer called {writer_call_count[0]} times, "
            f"expected at most {MAX_REVISION_CYCLES + 1}"
        )
