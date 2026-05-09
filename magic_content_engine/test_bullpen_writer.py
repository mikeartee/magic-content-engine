"""Tests for magic_content_engine/bullpen/writer.py.

Covers:
- Each output type produces the correct filename
- Voice rules compliance (no banned phrases, no em-dashes)
- revision_feedback incorporated when present
- WriterManifest JSON round-trip
- Property-based tests: output filename mapping invariant, voice rules scan
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import asdict
from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from magic_content_engine.bullpen.writer import (
    OUTPUT_TYPE_TO_FILENAME,
    ContentBrief,
    FileEntry,
    ScoredArticle,
    WriterInput,
    WriterManifest,
    _count_words,
    _inject_revision_feedback,
    _model_for,
    run_writer,
)
from magic_content_engine.writing_agent import VOICE_BANNED_PHRASES, validate_voice_rules


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_brief(
    output_types: list[str] | None = None,
    slug: str = "test-slug",
    run_date: str = "2025-07-14",
) -> ContentBrief:
    return ContentBrief(
        selected_articles=[
            ScoredArticle(
                title="AgentCore Runtime Launch",
                url="https://aws.amazon.com/agentcore",
                source="aws.amazon.com",
                relevance_score=5,
                summary="AWS launched AgentCore Runtime for agentic workloads.",
            )
        ],
        editorial_angle="Practical builder perspective on AgentCore",
        tone_guidance="Conversational, no banned phrases, no em-dashes",
        output_types=output_types or ["blog"],
        run_timestamp="2025-07-14T09:00:00+00:00",
        slug=slug,
        run_date=run_date,
    )


def _make_steering_files(tmp_path: pathlib.Path) -> None:
    """Write minimal steering files to tmp_path."""
    (tmp_path / "01-niche-and-voice.md").write_text(
        "Voice rules: conversational, no banned phrases, no em-dashes.",
        encoding="utf-8",
    )
    (tmp_path / "03-output-blog-post.md").write_text(
        "Blog post template: write a technical post.",
        encoding="utf-8",
    )
    (tmp_path / "04-output-youtube.md").write_text(
        "YouTube template: write a script.",
        encoding="utf-8",
    )
    (tmp_path / "05-output-talks.md").write_text(
        "Talks template: CFP and user group sessions.",
        encoding="utf-8",
    )


def _fake_llm(*, model_id: str, prompt: str) -> str:
    """Stub LLM that returns clean content without voice violations."""
    return (
        "This week AWS released new features for builders.\n\n"
        "The architecture uses three components working together.\n\n"
        "The build process takes about 30 minutes to complete.\n\n"
        "Results show a 40% reduction in latency for typical workloads."
    )


def _make_writer_input(
    tmp_path: pathlib.Path,
    output_types: list[str] | None = None,
    revision_feedback: str | None = None,
    slug: str = "test-slug",
) -> WriterInput:
    _make_steering_files(tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir(exist_ok=True)
    return WriterInput(
        content_brief=_make_brief(output_types=output_types, slug=slug),
        steering_base_path=str(tmp_path),
        output_dir=str(output_dir),
        revision_feedback=revision_feedback,
    )


# ---------------------------------------------------------------------------
# Output type → filename mapping
# ---------------------------------------------------------------------------


class TestOutputTypeFilenames:
    def test_blog_produces_post_md(self, tmp_path):
        wi = _make_writer_input(tmp_path, output_types=["blog"])
        manifest = run_writer(wi, _fake_llm)
        filenames = [pathlib.Path(f.path).name for f in manifest.files_written]
        assert "post.md" in filenames

    def test_youtube_produces_script_md(self, tmp_path):
        wi = _make_writer_input(tmp_path, output_types=["youtube"])
        manifest = run_writer(wi, _fake_llm)
        filenames = [pathlib.Path(f.path).name for f in manifest.files_written]
        assert "script.md" in filenames

    def test_youtube_produces_description_txt(self, tmp_path):
        wi = _make_writer_input(tmp_path, output_types=["youtube"])
        manifest = run_writer(wi, _fake_llm)
        filenames = [pathlib.Path(f.path).name for f in manifest.files_written]
        assert "description.txt" in filenames

    def test_cfp_produces_cfp_proposal_md(self, tmp_path):
        wi = _make_writer_input(tmp_path, output_types=["cfp"])
        manifest = run_writer(wi, _fake_llm)
        filenames = [pathlib.Path(f.path).name for f in manifest.files_written]
        assert "cfp-proposal.md" in filenames

    def test_usergroup_produces_usergroup_session_md(self, tmp_path):
        wi = _make_writer_input(tmp_path, output_types=["usergroup"])
        manifest = run_writer(wi, _fake_llm)
        filenames = [pathlib.Path(f.path).name for f in manifest.files_written]
        assert "usergroup-session.md" in filenames

    def test_digest_produces_digest_email_txt(self, tmp_path):
        wi = _make_writer_input(tmp_path, output_types=["digest"])
        manifest = run_writer(wi, _fake_llm)
        filenames = [pathlib.Path(f.path).name for f in manifest.files_written]
        assert "digest-email.txt" in filenames

    def test_all_five_output_types_produce_correct_files(self, tmp_path):
        wi = _make_writer_input(
            tmp_path, output_types=["blog", "youtube", "cfp", "usergroup", "digest"]
        )
        manifest = run_writer(wi, _fake_llm)
        filenames = {pathlib.Path(f.path).name for f in manifest.files_written}
        assert "post.md" in filenames
        assert "script.md" in filenames
        assert "description.txt" in filenames
        assert "cfp-proposal.md" in filenames
        assert "usergroup-session.md" in filenames
        assert "digest-email.txt" in filenames

    def test_output_bundle_dir_follows_date_slug_format(self, tmp_path):
        wi = _make_writer_input(tmp_path, output_types=["blog"], slug="agentcore-launch")
        manifest = run_writer(wi, _fake_llm)
        assert len(manifest.files_written) > 0
        first_path = pathlib.Path(manifest.files_written[0].path)
        # Should start with YYYY-MM-DD-slug directory
        assert first_path.parts[0] == "2025-07-14-agentcore-launch"

    def test_files_physically_exist_on_disk(self, tmp_path):
        wi = _make_writer_input(tmp_path, output_types=["blog", "digest"])
        manifest = run_writer(wi, _fake_llm)
        output_dir = pathlib.Path(str(tmp_path / "output"))
        for entry in manifest.files_written:
            full_path = output_dir / entry.path
            assert full_path.exists(), f"Expected file not found: {full_path}"

    def test_output_type_recorded_in_file_entry(self, tmp_path):
        wi = _make_writer_input(tmp_path, output_types=["blog"])
        manifest = run_writer(wi, _fake_llm)
        blog_entries = [f for f in manifest.files_written if f.output_type == "blog"]
        assert len(blog_entries) >= 1

    def test_word_count_is_positive(self, tmp_path):
        wi = _make_writer_input(tmp_path, output_types=["blog"])
        manifest = run_writer(wi, _fake_llm)
        for entry in manifest.files_written:
            assert entry.word_count > 0, f"word_count should be positive for {entry.path}"


# ---------------------------------------------------------------------------
# Voice rules compliance
# ---------------------------------------------------------------------------


class TestVoiceRulesCompliance:
    @pytest.mark.parametrize("phrase", VOICE_BANNED_PHRASES)
    def test_banned_phrase_in_llm_output_is_flagged(self, phrase: str):
        """validate_voice_rules catches banned phrases in generated content."""
        text = f"This tool will {phrase} your workflow significantly."
        violations = validate_voice_rules(text)
        assert any(phrase in v for v in violations)

    def test_clean_llm_output_passes_voice_rules(self):
        """The fake LLM output passes all voice rules."""
        content = _fake_llm(model_id="any", prompt="any")
        violations = validate_voice_rules(content)
        assert violations == []

    def test_em_dash_in_content_is_flagged(self):
        text = "This is a sentence\u2014with an em-dash in it."
        violations = validate_voice_rules(text)
        assert any("Em-dash" in v for v in violations)

    def test_no_em_dash_in_generated_output(self, tmp_path):
        """Files written by run_writer with clean LLM output contain no em-dashes."""
        wi = _make_writer_input(
            tmp_path, output_types=["blog", "cfp", "usergroup", "digest"]
        )
        manifest = run_writer(wi, _fake_llm)
        output_dir = pathlib.Path(str(tmp_path / "output"))
        for entry in manifest.files_written:
            content = (output_dir / entry.path).read_text(encoding="utf-8")
            assert "\u2014" not in content, (
                f"Em-dash found in {entry.path}"
            )
            assert "&#8212;" not in content, (
                f"HTML em-dash entity found in {entry.path}"
            )

    def test_no_banned_phrases_in_generated_output(self, tmp_path):
        """Files written by run_writer with clean LLM output contain no banned phrases."""
        wi = _make_writer_input(
            tmp_path, output_types=["blog", "cfp", "usergroup", "digest"]
        )
        manifest = run_writer(wi, _fake_llm)
        output_dir = pathlib.Path(str(tmp_path / "output"))
        for entry in manifest.files_written:
            content = (output_dir / entry.path).read_text(encoding="utf-8").lower()
            for phrase in VOICE_BANNED_PHRASES:
                assert phrase.lower() not in content, (
                    f"Banned phrase '{phrase}' found in {entry.path}"
                )

    def test_voice_rules_applied_is_always_true(self, tmp_path):
        wi = _make_writer_input(tmp_path, output_types=["blog"])
        manifest = run_writer(wi, _fake_llm)
        assert manifest.voice_rules_applied is True

    def test_mike_placeholder_format_in_blog(self, tmp_path):
        """Blog post contains properly formatted <!-- MIKE: --> placeholders."""
        wi = _make_writer_input(tmp_path, output_types=["blog"])
        manifest = run_writer(wi, _fake_llm)
        output_dir = pathlib.Path(str(tmp_path / "output"))
        post_entry = next(
            f for f in manifest.files_written if pathlib.Path(f.path).name == "post.md"
        )
        content = (output_dir / post_entry.path).read_text(encoding="utf-8")
        assert "<!-- MIKE:" in content

    def test_mike_placeholder_format_in_cfp(self, tmp_path):
        """CFP proposal contains properly formatted <!-- MIKE: --> placeholders."""
        wi = _make_writer_input(tmp_path, output_types=["cfp"])
        manifest = run_writer(wi, _fake_llm)
        output_dir = pathlib.Path(str(tmp_path / "output"))
        cfp_entry = next(
            f for f in manifest.files_written
            if pathlib.Path(f.path).name == "cfp-proposal.md"
        )
        content = (output_dir / cfp_entry.path).read_text(encoding="utf-8")
        assert "<!-- MIKE:" in content


# ---------------------------------------------------------------------------
# Revision feedback
# ---------------------------------------------------------------------------


class TestRevisionFeedback:
    def test_revision_feedback_injected_into_prompt(self):
        """_inject_revision_feedback appends feedback to the prompt."""
        original = "## Voice rules\n\nBe conversational."
        feedback = "The abstract is too long. Cut it to 200 words."
        result = _inject_revision_feedback(original, feedback)
        assert "Revision feedback from Subeditor" in result
        assert feedback in result
        assert original in result

    def test_revision_feedback_appears_after_original_prompt(self):
        original = "Original prompt content."
        feedback = "Please revise the opening paragraph."
        result = _inject_revision_feedback(original, feedback)
        original_pos = result.index(original)
        feedback_pos = result.index(feedback)
        assert original_pos < feedback_pos

    def test_run_writer_with_revision_feedback_succeeds(self, tmp_path):
        """run_writer completes successfully when revision_feedback is provided."""
        wi = _make_writer_input(
            tmp_path,
            output_types=["blog"],
            revision_feedback="The hook is too generic. Make it more specific to AgentCore.",
        )
        manifest = run_writer(wi, _fake_llm)
        assert len(manifest.files_written) == 1
        assert manifest.files_written[0].output_type == "blog"

    def test_run_writer_without_revision_feedback_succeeds(self, tmp_path):
        """run_writer completes successfully when revision_feedback is None."""
        wi = _make_writer_input(tmp_path, output_types=["blog"], revision_feedback=None)
        manifest = run_writer(wi, _fake_llm)
        assert len(manifest.files_written) == 1

    def test_revision_feedback_captured_in_prompt_for_all_types(self, tmp_path):
        """Verify revision feedback is passed through for each output type."""
        feedback = "Revise the opening section."
        captured_prompts: list[str] = []

        def capturing_llm(*, model_id: str, prompt: str) -> str:
            captured_prompts.append(prompt)
            return _fake_llm(model_id=model_id, prompt=prompt)

        for output_type in ["blog", "youtube", "cfp", "usergroup", "digest"]:
            captured_prompts.clear()
            wi = _make_writer_input(
                tmp_path,
                output_types=[output_type],
                revision_feedback=feedback,
            )
            run_writer(wi, capturing_llm)
            assert any(feedback in p for p in captured_prompts), (
                f"Revision feedback not found in prompt for output_type={output_type}"
            )


# ---------------------------------------------------------------------------
# WriterManifest JSON round-trip
# ---------------------------------------------------------------------------


class TestWriterManifestRoundTrip:
    def test_manifest_to_dict_and_back(self):
        manifest = WriterManifest(
            files_written=[
                FileEntry(path="2025-07-14-test/post.md", output_type="blog", word_count=450),
                FileEntry(path="2025-07-14-test/digest-email.txt", output_type="digest", word_count=120),
            ],
            voice_rules_applied=True,
            run_timestamp="2025-07-14T09:00:00+00:00",
        )
        data = manifest.to_dict()
        restored = WriterManifest.from_dict(data)

        assert restored.voice_rules_applied == manifest.voice_rules_applied
        assert restored.run_timestamp == manifest.run_timestamp
        assert len(restored.files_written) == len(manifest.files_written)
        for orig, rest in zip(manifest.files_written, restored.files_written):
            assert rest.path == orig.path
            assert rest.output_type == orig.output_type
            assert rest.word_count == orig.word_count

    def test_manifest_json_serialisable(self):
        manifest = WriterManifest(
            files_written=[
                FileEntry(path="2025-07-14-test/post.md", output_type="blog", word_count=300),
            ],
            voice_rules_applied=True,
            run_timestamp="2025-07-14T09:00:00+00:00",
        )
        # Should not raise
        serialised = json.dumps(manifest.to_dict())
        assert isinstance(serialised, str)

    def test_manifest_from_run_writer_is_round_trip_safe(self, tmp_path):
        wi = _make_writer_input(tmp_path, output_types=["blog", "digest"])
        manifest = run_writer(wi, _fake_llm)
        data = manifest.to_dict()
        restored = WriterManifest.from_dict(data)

        assert restored.voice_rules_applied == manifest.voice_rules_applied
        assert len(restored.files_written) == len(manifest.files_written)
        for orig, rest in zip(manifest.files_written, restored.files_written):
            assert rest.path == orig.path
            assert rest.output_type == orig.output_type
            assert rest.word_count == orig.word_count

    def test_manifest_run_timestamp_is_iso8601(self, tmp_path):
        wi = _make_writer_input(tmp_path, output_types=["blog"])
        manifest = run_writer(wi, _fake_llm)
        # Should parse without error
        from datetime import datetime
        dt = datetime.fromisoformat(manifest.run_timestamp)
        assert dt is not None

    def test_empty_manifest_round_trip(self):
        manifest = WriterManifest(
            files_written=[],
            voice_rules_applied=True,
            run_timestamp="2025-07-14T09:00:00+00:00",
        )
        restored = WriterManifest.from_dict(manifest.to_dict())
        assert restored.files_written == []
        assert restored.voice_rules_applied is True


# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------


class TestModelRouting:
    def test_blog_uses_sonnet(self):
        assert "sonnet" in _model_for("blog").lower()

    def test_youtube_uses_sonnet(self):
        assert "sonnet" in _model_for("youtube").lower()

    def test_cfp_uses_sonnet(self):
        assert "sonnet" in _model_for("cfp").lower()

    def test_usergroup_uses_sonnet(self):
        assert "sonnet" in _model_for("usergroup").lower()

    def test_digest_uses_haiku(self):
        assert "haiku" in _model_for("digest").lower()

    def test_model_used_for_digest_is_haiku(self, tmp_path):
        """Verify the LLM is called with a Haiku model ID for digest."""
        used_models: list[str] = []

        def recording_llm(*, model_id: str, prompt: str) -> str:
            used_models.append(model_id)
            return _fake_llm(model_id=model_id, prompt=prompt)

        wi = _make_writer_input(tmp_path, output_types=["digest"])
        run_writer(wi, recording_llm)
        assert len(used_models) == 1
        assert "haiku" in used_models[0].lower()

    def test_model_used_for_blog_is_sonnet(self, tmp_path):
        """Verify the LLM is called with a Sonnet model ID for blog."""
        used_models: list[str] = []

        def recording_llm(*, model_id: str, prompt: str) -> str:
            used_models.append(model_id)
            return _fake_llm(model_id=model_id, prompt=prompt)

        wi = _make_writer_input(tmp_path, output_types=["blog"])
        run_writer(wi, recording_llm)
        assert len(used_models) == 1
        assert "sonnet" in used_models[0].lower()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_unknown_output_type_skipped_gracefully(self, tmp_path):
        """Unknown output types are skipped; other types still succeed."""
        _make_steering_files(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        brief = _make_brief(output_types=["blog", "unknown_type"])
        wi = WriterInput(
            content_brief=brief,
            steering_base_path=str(tmp_path),
            output_dir=str(output_dir),
        )
        manifest = run_writer(wi, _fake_llm)
        # blog should succeed; unknown_type should be skipped
        filenames = [pathlib.Path(f.path).name for f in manifest.files_written]
        assert "post.md" in filenames

    def test_missing_steering_file_skips_output_type(self, tmp_path):
        """Missing steering file causes that output type to be skipped."""
        # Only write voice file, no template files
        (tmp_path / "01-niche-and-voice.md").write_text("Voice rules.", encoding="utf-8")
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        brief = _make_brief(output_types=["blog"])  # needs 03-output-blog-post.md
        wi = WriterInput(
            content_brief=brief,
            steering_base_path=str(tmp_path),
            output_dir=str(output_dir),
        )
        manifest = run_writer(wi, _fake_llm)
        # blog requires 03-output-blog-post.md which is missing
        filenames = [pathlib.Path(f.path).name for f in manifest.files_written]
        assert "post.md" not in filenames

    def test_llm_failure_skips_output_type(self, tmp_path):
        """LLM failure causes that output type to be skipped."""

        def failing_llm(*, model_id: str, prompt: str) -> str:
            raise RuntimeError("Bedrock unavailable")

        wi = _make_writer_input(tmp_path, output_types=["blog"])
        manifest = run_writer(wi, failing_llm)
        assert len(manifest.files_written) == 0

    def test_llm_failure_on_one_type_does_not_block_others(self, tmp_path):
        """LLM failure on blog does not prevent digest from being written."""
        call_count = [0]

        def selective_failing_llm(*, model_id: str, prompt: str) -> str:
            call_count[0] += 1
            if "blog" in prompt.lower() or "architecture" in prompt.lower():
                raise RuntimeError("Blog LLM failed")
            return _fake_llm(model_id=model_id, prompt=prompt)

        wi = _make_writer_input(tmp_path, output_types=["blog", "digest"])
        manifest = run_writer(wi, selective_failing_llm)
        filenames = [pathlib.Path(f.path).name for f in manifest.files_written]
        # digest should succeed even if blog fails
        assert "digest-email.txt" in filenames


# ---------------------------------------------------------------------------
# Word count helper
# ---------------------------------------------------------------------------


class TestCountWords:
    def test_simple_text(self):
        assert _count_words("hello world") == 2

    def test_excludes_comment_blocks(self):
        text = "<!-- MIKE: [Write a hook here] --> Some real content here."
        count = _count_words(text)
        # "Some real content here." = 4 words
        assert count == 4

    def test_empty_string(self):
        assert _count_words("") == 0

    def test_multiline_text(self):
        text = "Line one.\nLine two.\nLine three."
        assert _count_words(text) == 6


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


@given(
    output_type=st.sampled_from(["blog", "youtube", "cfp", "usergroup", "digest"])
)
@settings(max_examples=20)
def test_output_filename_mapping_invariant(output_type: str):
    """For every valid output type, OUTPUT_TYPE_TO_FILENAME contains the expected key."""
    # blog → post.md, youtube_script → script.md, etc.
    # The mapping covers the canonical sub-keys; output_type "youtube" maps to two files.
    if output_type == "blog":
        assert OUTPUT_TYPE_TO_FILENAME["blog"] == "post.md"
    elif output_type == "youtube":
        assert OUTPUT_TYPE_TO_FILENAME["youtube_script"] == "script.md"
        assert OUTPUT_TYPE_TO_FILENAME["youtube_description"] == "description.txt"
    elif output_type == "cfp":
        assert OUTPUT_TYPE_TO_FILENAME["cfp"] == "cfp-proposal.md"
    elif output_type == "usergroup":
        assert OUTPUT_TYPE_TO_FILENAME["usergroup"] == "usergroup-session.md"
    elif output_type == "digest":
        assert OUTPUT_TYPE_TO_FILENAME["digest"] == "digest-email.txt"


@given(
    text=st.text(
        alphabet=st.characters(
            whitelist_categories=("Lu", "Ll", "Nd", "Zs"),
            whitelist_characters=" .,\n",
        ),
        min_size=0,
        max_size=500,
    )
)
@settings(max_examples=50)
def test_voice_rules_compliance_scan_property(text: str):
    """validate_voice_rules always returns a list (never raises, never returns None)."""
    result = validate_voice_rules(text)
    assert isinstance(result, list)
    for violation in result:
        assert isinstance(violation, str)
        assert len(violation) > 0


@given(
    files=st.lists(
        st.builds(
            FileEntry,
            path=st.from_regex(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z]+/[a-z\-]+\.[a-z]+", fullmatch=True),
            output_type=st.sampled_from(["blog", "youtube", "cfp", "usergroup", "digest"]),
            word_count=st.integers(min_value=0, max_value=10000),
        ),
        min_size=0,
        max_size=10,
    ),
    voice_rules_applied=st.just(True),
    run_timestamp=st.from_regex(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+00:00",
        fullmatch=True,
    ),
)
@settings(max_examples=50)
def test_writer_manifest_json_round_trip_property(
    files: list[FileEntry],
    voice_rules_applied: bool,
    run_timestamp: str,
):
    """WriterManifest serialises to dict and back without data loss."""
    manifest = WriterManifest(
        files_written=files,
        voice_rules_applied=voice_rules_applied,
        run_timestamp=run_timestamp,
    )
    data = manifest.to_dict()
    restored = WriterManifest.from_dict(data)

    assert restored.voice_rules_applied == manifest.voice_rules_applied
    assert restored.run_timestamp == manifest.run_timestamp
    assert len(restored.files_written) == len(manifest.files_written)
    for orig, rest in zip(manifest.files_written, restored.files_written):
        assert rest.path == orig.path
        assert rest.output_type == orig.output_type
        assert rest.word_count == orig.word_count


@given(
    banned_phrase=st.sampled_from(VOICE_BANNED_PHRASES),
    prefix=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Zs")),
        min_size=1,
        max_size=50,
    ),
    suffix=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Zs")),
        min_size=1,
        max_size=50,
    ),
)
@settings(max_examples=50)
def test_banned_phrase_always_detected_property(
    banned_phrase: str, prefix: str, suffix: str
):
    """Any text containing a banned phrase is always flagged by validate_voice_rules."""
    text = f"{prefix} {banned_phrase} {suffix}"
    violations = validate_voice_rules(text)
    assert any(banned_phrase.lower() in v.lower() for v in violations), (
        f"Expected '{banned_phrase}' to be flagged in: {text!r}"
    )
