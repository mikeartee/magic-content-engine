"""Tests for the Subeditor Lambda.

Covers:
- Verdict completeness (one per file)
- Feedback non-empty for revise/spike
- Voice rule violation detection
- SubeditorReview JSON round-trip
- Property-based tests: verdict completeness invariant, feedback non-empty for revise/spike

Requirements: REQ-9.4–REQ-9.6, REQ-10.2–REQ-10.3
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import tempfile

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from magic_content_engine.bullpen.models import (
    FileEntry,
    SubeditorReview,
    Verdict,
    WriterManifest,
)
from magic_content_engine.bullpen.subeditor import (
    VOICE_BANNED_PHRASES,
    _parse_verdict_response,
    check_voice_rules,
    review,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VOICE_RULES_CONTENT = """\
---
title: "Niche focus and voice"
---
## Voice rules
- No em-dashes
- Short sentences preferred
- Do not open with "I"
- Never use: leverage, empower, unlock, dive into, game-changer
"""


def _make_llm(verdict: str, feedback: str = ""):
    """Return a fake LLM that always responds with the given verdict/feedback."""

    def _call(*, model_id: str, prompt: str) -> str:
        return json.dumps({"verdict": verdict, "feedback": feedback})

    return _call


def _make_manifest(*filenames: str, output_type: str = "blog") -> WriterManifest:
    """Build a WriterManifest with the given filenames."""
    entries = [
        FileEntry(path=name, output_type=output_type, word_count=100)
        for name in filenames
    ]
    return WriterManifest(files_written=entries, run_timestamp="2025-07-14T00:00:00+00:00")


def _write_files(tmp_dir: pathlib.Path, filenames: list[str], content: str = "Good content.") -> None:
    """Write content files into tmp_dir."""
    for name in filenames:
        (tmp_dir / name).write_text(content, encoding="utf-8")


def _write_voice_rules(tmp_dir: pathlib.Path) -> None:
    """Write the canonical voice rules steering file into tmp_dir."""
    (tmp_dir / "01-niche-and-voice.md").write_text(VOICE_RULES_CONTENT, encoding="utf-8")


# ---------------------------------------------------------------------------
# check_voice_rules
# ---------------------------------------------------------------------------


class TestCheckVoiceRules:
    def test_clean_text_returns_no_violations(self):
        assert check_voice_rules("This is clean content about AWS.") == []

    @pytest.mark.parametrize("phrase", VOICE_BANNED_PHRASES)
    def test_banned_phrase_detected(self, phrase: str):
        violations = check_voice_rules(f"We can {phrase} the platform.")
        assert any(phrase in v for v in violations)

    def test_banned_phrase_case_insensitive(self):
        violations = check_voice_rules("We can LEVERAGE the platform.")
        assert any("leverage" in v.lower() for v in violations)

    def test_em_dash_unicode_detected(self):
        violations = check_voice_rules("This is great\u2014really great.")
        assert any("em-dash" in v.lower() for v in violations)

    def test_em_dash_html_entity_detected(self):
        violations = check_voice_rules("This is great&#8212;really great.")
        assert any("em-dash" in v.lower() for v in violations)

    def test_paragraph_opening_with_i_detected(self):
        violations = check_voice_rules("I built this system last week.")
        assert any("'I'" in v or "opens with" in v.lower() for v in violations)

    def test_paragraph_opening_with_i_after_blank_line(self):
        text = "First paragraph.\n\nI started building this."
        violations = check_voice_rules(text)
        assert any("opens with" in v.lower() or "'I'" in v for v in violations)

    def test_i_mid_sentence_not_flagged(self):
        violations = check_voice_rules("The system that I built works well.")
        assert not any("opens with" in v.lower() for v in violations)

    def test_malformed_mike_placeholder_detected(self):
        # Missing brackets around instruction
        violations = check_voice_rules("<!-- MIKE: write something here -->")
        assert any("MIKE" in v for v in violations)

    def test_valid_mike_placeholder_not_flagged(self):
        violations = check_voice_rules("<!-- MIKE: [Write a hook, ~100 words] -->")
        assert not any("MIKE" in v for v in violations)

    def test_multiple_violations_all_reported(self):
        text = "I leverage the platform\u2014it's a game-changer."
        violations = check_voice_rules(text)
        assert len(violations) >= 3  # I-open, leverage, em-dash, game-changer


# ---------------------------------------------------------------------------
# _parse_verdict_response
# ---------------------------------------------------------------------------


class TestParseVerdictResponse:
    def test_publish_verdict_parsed(self):
        raw = json.dumps({"verdict": "publish", "feedback": ""})
        v = _parse_verdict_response(raw, "post.md")
        assert v.verdict == "publish"
        assert v.feedback == ""

    def test_revise_verdict_parsed(self):
        raw = json.dumps({"verdict": "revise", "feedback": "Fix the em-dash on line 3."})
        v = _parse_verdict_response(raw, "post.md")
        assert v.verdict == "revise"
        assert v.feedback == "Fix the em-dash on line 3."

    def test_spike_verdict_parsed(self):
        raw = json.dumps({"verdict": "spike", "feedback": "Off-topic content."})
        v = _parse_verdict_response(raw, "post.md")
        assert v.verdict == "spike"
        assert v.feedback == "Off-topic content."

    def test_invalid_json_falls_back_to_revise(self):
        v = _parse_verdict_response("not json at all", "post.md")
        assert v.verdict == "revise"
        assert v.feedback  # non-empty

    def test_unknown_verdict_falls_back_to_revise(self):
        raw = json.dumps({"verdict": "hold", "feedback": ""})
        v = _parse_verdict_response(raw, "post.md")
        assert v.verdict == "revise"
        assert v.feedback  # non-empty

    def test_revise_without_feedback_gets_placeholder(self):
        raw = json.dumps({"verdict": "revise", "feedback": ""})
        v = _parse_verdict_response(raw, "post.md")
        assert v.verdict == "revise"
        assert v.feedback  # non-empty fallback

    def test_spike_without_feedback_gets_placeholder(self):
        raw = json.dumps({"verdict": "spike", "feedback": ""})
        v = _parse_verdict_response(raw, "post.md")
        assert v.verdict == "spike"
        assert v.feedback  # non-empty fallback

    def test_markdown_fenced_json_parsed(self):
        raw = "```json\n" + json.dumps({"verdict": "publish", "feedback": ""}) + "\n```"
        v = _parse_verdict_response(raw, "post.md")
        assert v.verdict == "publish"

    def test_filename_preserved(self):
        raw = json.dumps({"verdict": "publish", "feedback": ""})
        v = _parse_verdict_response(raw, "script.md")
        assert v.filename == "script.md"


# ---------------------------------------------------------------------------
# review() — verdict completeness
# ---------------------------------------------------------------------------


class TestReviewVerdictCompleteness:
    def test_one_verdict_per_file(self, tmp_path):
        filenames = ["post.md", "script.md", "cfp-proposal.md"]
        _write_files(tmp_path, filenames)
        _write_voice_rules(tmp_path)

        manifest = _make_manifest(*filenames)
        result = review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_make_llm("publish"),
            steering_base_path=str(tmp_path),
        )

        assert len(result.verdicts) == len(filenames)
        verdict_filenames = {v.filename for v in result.verdicts}
        assert verdict_filenames == set(filenames)

    def test_empty_manifest_returns_empty_verdicts(self, tmp_path):
        _write_voice_rules(tmp_path)
        manifest = WriterManifest(files_written=[], run_timestamp="2025-07-14T00:00:00+00:00")
        result = review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_make_llm("publish"),
            steering_base_path=str(tmp_path),
        )
        assert result.verdicts == []

    def test_missing_file_gets_spike_verdict(self, tmp_path):
        _write_voice_rules(tmp_path)
        manifest = _make_manifest("missing.md")
        result = review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_make_llm("publish"),
            steering_base_path=str(tmp_path),
        )
        assert len(result.verdicts) == 1
        assert result.verdicts[0].verdict == "spike"
        assert result.verdicts[0].feedback  # non-empty rationale

    def test_run_timestamp_present(self, tmp_path):
        _write_voice_rules(tmp_path)
        (tmp_path / "post.md").write_text("Good content.", encoding="utf-8")
        manifest = _make_manifest("post.md")
        result = review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_make_llm("publish"),
            steering_base_path=str(tmp_path),
        )
        assert result.run_timestamp  # non-empty ISO 8601 string


# ---------------------------------------------------------------------------
# review() — feedback non-empty for revise/spike
# ---------------------------------------------------------------------------


class TestReviewFeedbackContract:
    def test_publish_verdict_has_empty_feedback(self, tmp_path):
        _write_voice_rules(tmp_path)
        (tmp_path / "post.md").write_text("Good content.", encoding="utf-8")
        manifest = _make_manifest("post.md")
        result = review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_make_llm("publish", ""),
            steering_base_path=str(tmp_path),
        )
        assert result.verdicts[0].verdict == "publish"
        assert result.verdicts[0].feedback == ""

    def test_revise_verdict_has_non_empty_feedback(self, tmp_path):
        _write_voice_rules(tmp_path)
        (tmp_path / "post.md").write_text("Good content.", encoding="utf-8")
        manifest = _make_manifest("post.md")
        result = review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_make_llm("revise", "Remove the em-dash on line 5."),
            steering_base_path=str(tmp_path),
        )
        assert result.verdicts[0].verdict == "revise"
        assert result.verdicts[0].feedback.strip()

    def test_spike_verdict_has_non_empty_feedback(self, tmp_path):
        _write_voice_rules(tmp_path)
        (tmp_path / "post.md").write_text("Good content.", encoding="utf-8")
        manifest = _make_manifest("post.md")
        result = review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_make_llm("spike", "Content is off-topic and cannot be salvaged."),
            steering_base_path=str(tmp_path),
        )
        assert result.verdicts[0].verdict == "spike"
        assert result.verdicts[0].feedback.strip()

    def test_llm_failure_produces_revise_with_feedback(self, tmp_path):
        _write_voice_rules(tmp_path)
        (tmp_path / "post.md").write_text("Good content.", encoding="utf-8")
        manifest = _make_manifest("post.md")

        def _failing_llm(*, model_id: str, prompt: str) -> str:
            raise RuntimeError("Bedrock timeout")

        result = review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_failing_llm,
            steering_base_path=str(tmp_path),
        )
        assert result.verdicts[0].verdict == "revise"
        assert result.verdicts[0].feedback.strip()


# ---------------------------------------------------------------------------
# review() — voice rule violation detection
# ---------------------------------------------------------------------------


class TestReviewVoiceRuleDetection:
    def test_content_with_banned_phrase_triggers_revise(self, tmp_path):
        """LLM is told about the violation; we verify the prompt includes it."""
        _write_voice_rules(tmp_path)
        (tmp_path / "post.md").write_text(
            "We can leverage the platform to build faster.", encoding="utf-8"
        )
        manifest = _make_manifest("post.md")

        prompts_seen: list[str] = []

        def _capturing_llm(*, model_id: str, prompt: str) -> str:
            prompts_seen.append(prompt)
            return json.dumps({"verdict": "revise", "feedback": "Remove 'leverage'."})

        result = review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_capturing_llm,
            steering_base_path=str(tmp_path),
        )

        assert len(prompts_seen) == 1
        assert "leverage" in prompts_seen[0].lower()
        assert result.verdicts[0].verdict == "revise"

    def test_content_with_em_dash_violation_in_prompt(self, tmp_path):
        _write_voice_rules(tmp_path)
        (tmp_path / "post.md").write_text(
            "This is great\u2014really great content.", encoding="utf-8"
        )
        manifest = _make_manifest("post.md")

        prompts_seen: list[str] = []

        def _capturing_llm(*, model_id: str, prompt: str) -> str:
            prompts_seen.append(prompt)
            return json.dumps({"verdict": "revise", "feedback": "Remove em-dash."})

        review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_capturing_llm,
            steering_base_path=str(tmp_path),
        )

        assert "em-dash" in prompts_seen[0].lower()

    def test_clean_content_passes_voice_check(self, tmp_path):
        _write_voice_rules(tmp_path)
        (tmp_path / "post.md").write_text(
            "Building AI tooling on AWS from Aotearoa.", encoding="utf-8"
        )
        manifest = _make_manifest("post.md")

        prompts_seen: list[str] = []

        def _capturing_llm(*, model_id: str, prompt: str) -> str:
            prompts_seen.append(prompt)
            return json.dumps({"verdict": "publish", "feedback": ""})

        result = review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_capturing_llm,
            steering_base_path=str(tmp_path),
        )

        assert "No automatic voice rule violations detected" in prompts_seen[0]
        assert result.verdicts[0].verdict == "publish"

    def test_missing_steering_file_raises(self, tmp_path):
        manifest = _make_manifest("post.md")
        with pytest.raises(FileNotFoundError, match="Voice rules steering file not found"):
            review(
                manifest=manifest,
                output_dir=str(tmp_path),
                llm=_make_llm("publish"),
                steering_base_path=str(tmp_path),
            )

    def test_voice_rules_read_from_steering_path(self, tmp_path):
        """Steering file is read at runtime from the given path."""
        custom_steering = tmp_path / "custom_steering"
        custom_steering.mkdir()
        (custom_steering / "01-niche-and-voice.md").write_text(
            "Custom voice rules content.", encoding="utf-8"
        )
        (tmp_path / "post.md").write_text("Good content.", encoding="utf-8")
        manifest = _make_manifest("post.md")

        prompts_seen: list[str] = []

        def _capturing_llm(*, model_id: str, prompt: str) -> str:
            prompts_seen.append(prompt)
            return json.dumps({"verdict": "publish", "feedback": ""})

        review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_capturing_llm,
            steering_base_path=str(custom_steering),
        )

        assert "Custom voice rules content." in prompts_seen[0]


# ---------------------------------------------------------------------------
# SubeditorReview JSON round-trip
# ---------------------------------------------------------------------------


class TestSubeditorReviewJsonRoundTrip:
    def test_round_trip_with_verdicts(self):
        review_obj = SubeditorReview(
            verdicts=[
                Verdict(filename="post.md", verdict="publish", feedback=""),
                Verdict(filename="script.md", verdict="revise", feedback="Fix em-dash."),
                Verdict(filename="cfp.md", verdict="spike", feedback="Off-topic."),
            ],
            run_timestamp="2025-07-14T09:00:00+00:00",
        )

        serialised = json.dumps(dataclasses.asdict(review_obj))
        deserialised = json.loads(serialised)

        restored = SubeditorReview(
            verdicts=[
                Verdict(**v) for v in deserialised["verdicts"]
            ],
            run_timestamp=deserialised["run_timestamp"],
        )

        assert restored == review_obj

    def test_round_trip_empty_verdicts(self):
        review_obj = SubeditorReview(verdicts=[], run_timestamp="2025-07-14T09:00:00+00:00")
        serialised = json.dumps(dataclasses.asdict(review_obj))
        deserialised = json.loads(serialised)
        restored = SubeditorReview(
            verdicts=[Verdict(**v) for v in deserialised["verdicts"]],
            run_timestamp=deserialised["run_timestamp"],
        )
        assert restored == review_obj

    def test_writer_manifest_round_trip(self):
        manifest = WriterManifest(
            files_written=[
                FileEntry(path="post.md", output_type="blog", word_count=800),
                FileEntry(path="script.md", output_type="youtube", word_count=1200),
            ],
            voice_rules_applied=True,
            run_timestamp="2025-07-14T08:00:00+00:00",
        )
        serialised = json.dumps(dataclasses.asdict(manifest))
        deserialised = json.loads(serialised)
        restored = WriterManifest(
            files_written=[FileEntry(**e) for e in deserialised["files_written"]],
            voice_rules_applied=deserialised["voice_rules_applied"],
            run_timestamp=deserialised["run_timestamp"],
        )
        assert restored == manifest


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------

# Strategy for valid verdict strings
_verdict_strategy = st.sampled_from(["publish", "revise", "spike"])

# Strategy for non-empty feedback strings
_feedback_strategy = st.text(min_size=1, max_size=200).filter(str.strip)

# Strategy for filenames
_filename_strategy = st.from_regex(r"[a-z][a-z0-9\-]{0,20}\.(md|txt)", fullmatch=True)


@st.composite
def file_entries(draw) -> list[FileEntry]:
    """Generate a list of 0–10 unique FileEntry objects."""
    n = draw(st.integers(min_value=0, max_value=10))
    names = draw(
        st.lists(
            _filename_strategy,
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    return [
        FileEntry(
            path=name,
            output_type=draw(st.sampled_from(["blog", "youtube", "cfp", "usergroup", "digest"])),
            word_count=draw(st.integers(min_value=0, max_value=5000)),
        )
        for name in names
    ]


@given(entries=file_entries())
@settings(max_examples=100)
def test_property_verdict_completeness(entries: list[FileEntry]):
    """Property: SubeditorReview always has exactly one Verdict per FileEntry."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = pathlib.Path(tmp_dir)
        (tmp_path / "01-niche-and-voice.md").write_text(VOICE_RULES_CONTENT, encoding="utf-8")

        # Write all files
        for entry in entries:
            (tmp_path / entry.path).write_text("Good content about AWS.", encoding="utf-8")

        manifest = WriterManifest(files_written=entries, run_timestamp="2025-07-14T00:00:00+00:00")
        result = review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_make_llm("publish"),
            steering_base_path=str(tmp_path),
        )

        assert len(result.verdicts) == len(entries)


@given(
    verdict=_verdict_strategy,
    feedback=st.one_of(st.just(""), _feedback_strategy),
)
@settings(max_examples=200)
def test_property_feedback_non_empty_for_revise_spike(
    verdict: str, feedback: str
):
    """Property: any Verdict with verdict=revise or spike always has non-empty feedback."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = pathlib.Path(tmp_dir)
        (tmp_path / "01-niche-and-voice.md").write_text(VOICE_RULES_CONTENT, encoding="utf-8")
        (tmp_path / "post.md").write_text("Good content.", encoding="utf-8")

        manifest = _make_manifest("post.md")
        result = review(
            manifest=manifest,
            output_dir=str(tmp_path),
            llm=_make_llm(verdict, feedback),
            steering_base_path=str(tmp_path),
        )

        assert len(result.verdicts) == 1
        v = result.verdicts[0]
        assert v.verdict in ("publish", "revise", "spike")

        if v.verdict in ("revise", "spike"):
            assert v.feedback.strip(), (
                f"Verdict '{v.verdict}' must have non-empty feedback, got: {v.feedback!r}"
            )
