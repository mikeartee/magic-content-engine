"""Tests for the Writing_Sub_Agent core framework.

Covers: WritingContext, LLMProtocol, voice-rule validation,
OUTPUT_TYPE_TO_TASK mapping, and generate_content orchestration.
"""

from __future__ import annotations

from datetime import date

import pytest

from magic_content_engine.errors import ErrorCollector
from magic_content_engine.model_router import TaskType, get_model
from magic_content_engine.models import APACitation, Article, ArticleMetadata
from magic_content_engine.writing_agent import (
    OUTPUT_TYPE_TO_TASK,
    VOICE_BANNED_PHRASES,
    ArticleWithCitation,
    WritingContext,
    assemble_blog_post,
    assemble_cfp_proposal,
    assemble_digest_email,
    assemble_usergroup_session,
    assemble_youtube_description,
    assemble_youtube_script,
    build_blog_prompt,
    build_cfp_prompt,
    build_digest_prompt,
    build_usergroup_prompt,
    build_youtube_prompt,
    generate_content,
    validate_voice_rules,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_article_with_citation() -> ArticleWithCitation:
    article = Article(
        url="https://example.com/post",
        title="Test Article",
        source="example.com",
        source_type="primary",
        discovered_date=date(2025, 7, 14),
        relevance_score=4,
        status="confirmed",
    )
    metadata = ArticleMetadata(
        article_url=article.url,
        title=article.title,
        author="Test Author",
        publisher="Example",
    )
    citation = APACitation(
        metadata=metadata,
        reference_entry="Author, T. (2025). Test. Example. https://example.com/post",
        in_text_citation="(Author, 2025)",
        bibtex_entry="@online{author2025, ...}",
    )
    return ArticleWithCitation(article=article, citation=citation)


def _make_context(output_type: str = "blog", tmp_path: str = "/tmp") -> WritingContext:
    return WritingContext(
        articles=[_make_article_with_citation()],
        output_type=output_type,
        steering_base_path=tmp_path,
        screenshots_path="screenshots/",
        run_date=date(2025, 7, 14),
        slug="test-slug",
    )


def _fake_llm(*, model_id: str, prompt: str) -> str:
    """Stub LLM that returns clean content."""
    return "This is generated content about AWS services.\n\nThe results look good."


def _failing_llm(*, model_id: str, prompt: str) -> str:
    raise RuntimeError("LLM service unavailable")


# ---------------------------------------------------------------------------
# validate_voice_rules
# ---------------------------------------------------------------------------


class TestValidateVoiceRules:
    def test_clean_text_passes(self):
        text = "This is a clean paragraph about AWS.\n\nThe results are great."
        assert validate_voice_rules(text) == []

    @pytest.mark.parametrize("phrase", VOICE_BANNED_PHRASES)
    def test_banned_phrase_detected(self, phrase: str):
        text = f"We will {phrase} the community with this tool."
        violations = validate_voice_rules(text)
        assert any(phrase in v for v in violations)

    def test_banned_phrase_case_insensitive(self):
        text = "Let's LEVERAGE this opportunity."
        violations = validate_voice_rules(text)
        assert any("leverage" in v for v in violations)

    def test_em_dash_unicode_detected(self):
        text = "This is a sentence\u2014with an em-dash."
        violations = validate_voice_rules(text)
        assert any("Em-dash" in v for v in violations)

    def test_em_dash_html_entity_detected(self):
        text = "This is a sentence&#8212;with an em-dash."
        violations = validate_voice_rules(text)
        assert any("Em-dash" in v for v in violations)

    def test_paragraph_opening_with_I(self):
        text = "Some intro.\n\nI started building this last week."
        violations = validate_voice_rules(text)
        assert any("opens with 'I'" in v for v in violations)

    def test_text_start_with_I(self):
        text = "I built this tool last week."
        violations = validate_voice_rules(text)
        assert any("opens with 'I'" in v for v in violations)

    def test_I_mid_sentence_is_fine(self):
        text = "The tool that I built works well."
        assert validate_voice_rules(text) == []

    def test_multiple_violations(self):
        text = "I want to leverage this game-changer\u2014it's great."
        violations = validate_voice_rules(text)
        assert len(violations) >= 3


# ---------------------------------------------------------------------------
# OUTPUT_TYPE_TO_TASK mapping
# ---------------------------------------------------------------------------


class TestOutputTypeToTask:
    def test_narrative_types_map_to_sonnet_tasks(self):
        assert OUTPUT_TYPE_TO_TASK["blog"] == TaskType.BLOG_POST
        assert OUTPUT_TYPE_TO_TASK["youtube"] == TaskType.YOUTUBE_SCRIPT
        assert OUTPUT_TYPE_TO_TASK["cfp"] == TaskType.CFP_ABSTRACT
        assert OUTPUT_TYPE_TO_TASK["usergroup"] == TaskType.USERGROUP_OUTLINE

    def test_digest_maps_to_haiku_task(self):
        assert OUTPUT_TYPE_TO_TASK["digest"] == TaskType.DIGEST_EMAIL

    def test_all_five_output_types_present(self):
        expected = {"blog", "youtube", "cfp", "usergroup", "digest"}
        assert set(OUTPUT_TYPE_TO_TASK.keys()) == expected

    def test_narrative_outputs_route_to_sonnet(self):
        for otype in ("blog", "youtube", "cfp", "usergroup"):
            task = OUTPUT_TYPE_TO_TASK[otype]
            model = get_model(task)
            assert "sonnet" in model.lower(), f"{otype} should route to Sonnet"

    def test_digest_routes_to_haiku(self):
        task = OUTPUT_TYPE_TO_TASK["digest"]
        model = get_model(task)
        assert "haiku" in model.lower()


# ---------------------------------------------------------------------------
# generate_content
# ---------------------------------------------------------------------------


class TestGenerateContent:
    def test_success_returns_content(self, tmp_path):
        # Create minimal steering files
        voice = tmp_path / "01-niche-and-voice.md"
        voice.write_text("Voice rules: be conversational.", encoding="utf-8")
        template = tmp_path / "03-output-blog-post.md"
        template.write_text("Blog template here.", encoding="utf-8")

        ctx = _make_context("blog", str(tmp_path))
        collector = ErrorCollector()

        result = generate_content(ctx, _fake_llm, collector)

        assert result is not None
        assert "generated content" in result
        assert not collector.has_errors

    def test_missing_steering_file_returns_none(self):
        ctx = _make_context("blog", "/nonexistent/path")
        collector = ErrorCollector()

        result = generate_content(ctx, _fake_llm, collector)

        assert result is None
        assert collector.has_errors
        assert any("Steering file missing" in e.error_message for e in collector.errors)

    def test_llm_failure_returns_none(self, tmp_path):
        voice = tmp_path / "01-niche-and-voice.md"
        voice.write_text("Voice rules.", encoding="utf-8")
        template = tmp_path / "03-output-blog-post.md"
        template.write_text("Blog template.", encoding="utf-8")

        ctx = _make_context("blog", str(tmp_path))
        collector = ErrorCollector()

        result = generate_content(ctx, _failing_llm, collector)

        assert result is None
        assert collector.has_errors
        assert any("LLM service unavailable" in e.error_message for e in collector.errors)

    def test_unknown_output_type_returns_none(self, tmp_path):
        voice = tmp_path / "01-niche-and-voice.md"
        voice.write_text("Voice rules.", encoding="utf-8")

        ctx = _make_context("unknown_type", str(tmp_path))
        collector = ErrorCollector()

        result = generate_content(ctx, _fake_llm, collector)

        assert result is None
        assert collector.has_errors
        assert any("Unknown output type" in e.error_message for e in collector.errors)

    def test_digest_uses_voice_only(self, tmp_path):
        voice = tmp_path / "01-niche-and-voice.md"
        voice.write_text("Voice rules for digest.", encoding="utf-8")

        ctx = _make_context("digest", str(tmp_path))
        collector = ErrorCollector()

        result = generate_content(ctx, _fake_llm, collector)

        assert result is not None
        assert not collector.has_errors

    def test_voice_violations_logged_but_content_returned(self, tmp_path):
        voice = tmp_path / "01-niche-and-voice.md"
        voice.write_text("Voice rules.", encoding="utf-8")
        template = tmp_path / "03-output-blog-post.md"
        template.write_text("Blog template.", encoding="utf-8")

        def llm_with_violations(*, model_id: str, prompt: str) -> str:
            return "I want to leverage this game-changer."

        ctx = _make_context("blog", str(tmp_path))
        collector = ErrorCollector()

        result = generate_content(ctx, llm_with_violations, collector)

        # Content is still returned (violations are warnings, not errors)
        assert result is not None
        assert "leverage" in result

    def test_error_collector_continues_after_failure(self, tmp_path):
        """Verify the collector pattern: failure on one output doesn't block others."""
        collector = ErrorCollector()

        # First call fails (missing steering)
        ctx1 = _make_context("blog", "/nonexistent")
        result1 = generate_content(ctx1, _fake_llm, collector)
        assert result1 is None

        # Second call succeeds
        voice = tmp_path / "01-niche-and-voice.md"
        voice.write_text("Voice.", encoding="utf-8")
        ctx2 = _make_context("digest", str(tmp_path))
        result2 = generate_content(ctx2, _fake_llm, collector)
        assert result2 is not None

        # Collector has exactly one error from the first call
        assert len(collector.errors) == 1


# ---------------------------------------------------------------------------
# build_blog_prompt
# ---------------------------------------------------------------------------


class TestBuildBlogPrompt:
    def test_includes_voice_rules(self):
        ctx = _make_context("blog")
        steering = {"voice": "Be conversational.", "template": "Blog template."}
        prompt = build_blog_prompt(ctx, steering)
        assert "Be conversational." in prompt

    def test_includes_template(self):
        ctx = _make_context("blog")
        steering = {"voice": "Voice.", "template": "Blog structure here."}
        prompt = build_blog_prompt(ctx, steering)
        assert "Blog structure here." in prompt

    def test_includes_article_citations(self):
        ctx = _make_context("blog")
        steering = {"voice": "Voice.", "template": "Template."}
        prompt = build_blog_prompt(ctx, steering)
        assert "(Author, 2025)" in prompt
        assert "Test Article" in prompt

    def test_includes_generation_instructions(self):
        ctx = _make_context("blog")
        steering = {"voice": "Voice."}
        prompt = build_blog_prompt(ctx, steering)
        assert "Architecture section" in prompt
        assert "Build walkthrough" in prompt
        assert "Cost breakdown" in prompt
        assert "Sample output" in prompt

    def test_includes_run_metadata(self):
        ctx = _make_context("blog")
        steering = {"voice": "Voice."}
        prompt = build_blog_prompt(ctx, steering)
        assert "2025-07-14" in prompt
        assert "test-slug" in prompt

    def test_no_template_key_still_works(self):
        ctx = _make_context("blog")
        steering = {"voice": "Voice only."}
        prompt = build_blog_prompt(ctx, steering)
        assert "Voice only." in prompt
        assert "Output template" not in prompt


# ---------------------------------------------------------------------------
# assemble_blog_post
# ---------------------------------------------------------------------------


class TestAssembleBlogPost:
    def test_contains_hook_placeholder(self):
        ctx = _make_context("blog")
        result = assemble_blog_post(ctx, "Body content here.")
        assert "<!-- MIKE: [Write a personal hook." in result
        assert "~100 words" in result

    def test_hook_includes_suggested_angles(self):
        ctx = _make_context("blog")
        result = assemble_blog_post(ctx, "Body.")
        assert "Test Article" in result

    def test_contains_generated_body(self):
        ctx = _make_context("blog")
        body = "## Architecture\n\nThe system uses AgentCore.\n\n## Build\n\nStep one."
        result = assemble_blog_post(ctx, body)
        assert "The system uses AgentCore." in result
        assert "Step one." in result

    def test_contains_oceania_placeholder(self):
        ctx = _make_context("blog")
        result = assemble_blog_post(ctx, "Body.")
        assert "<!-- MIKE: [Aotearoa angle, ~60 words] -->" in result

    def test_contains_closing_placeholder(self):
        ctx = _make_context("blog")
        result = assemble_blog_post(ctx, "Body.")
        assert "<!-- MIKE: [Closing with CTA and GitHub link, ~50 words] -->" in result

    def test_contains_references_section(self):
        ctx = _make_context("blog")
        result = assemble_blog_post(ctx, "Body.")
        assert "## References" in result
        assert "Author, T. (2025). Test. Example." in result

    def test_references_sorted_alphabetically(self):
        """With multiple articles, references should be sorted by entry."""
        article_a = Article(
            url="https://a.com",
            title="Alpha Article",
            source="a.com",
            source_type="primary",
            discovered_date=date(2025, 7, 14),
            relevance_score=5,
            status="confirmed",
        )
        meta_a = ArticleMetadata(article_url="https://a.com", title="Alpha Article", author="Zeta Author")
        cite_a = APACitation(
            metadata=meta_a,
            reference_entry="Zeta Author. (2025). Alpha Article. A.com. https://a.com",
            in_text_citation="(Zeta Author, 2025)",
            bibtex_entry="@online{zeta2025, ...}",
        )

        article_b = Article(
            url="https://b.com",
            title="Beta Article",
            source="b.com",
            source_type="primary",
            discovered_date=date(2025, 7, 14),
            relevance_score=4,
            status="confirmed",
        )
        meta_b = ArticleMetadata(article_url="https://b.com", title="Beta Article", author="Alpha Author")
        cite_b = APACitation(
            metadata=meta_b,
            reference_entry="Alpha Author. (2025). Beta Article. B.com. https://b.com",
            in_text_citation="(Alpha Author, 2025)",
            bibtex_entry="@online{alpha2025, ...}",
        )

        ctx = WritingContext(
            articles=[
                ArticleWithCitation(article=article_a, citation=cite_a),
                ArticleWithCitation(article=article_b, citation=cite_b),
            ],
            output_type="blog",
            steering_base_path="/tmp",
            screenshots_path="screenshots/",
            run_date=date(2025, 7, 14),
            slug="test-slug",
        )

        result = assemble_blog_post(ctx, "Body.")
        alpha_pos = result.index("Alpha Author")
        zeta_pos = result.index("Zeta Author")
        assert alpha_pos < zeta_pos, "References should be sorted alphabetically"

    def test_contains_title(self):
        ctx = _make_context("blog")
        result = assemble_blog_post(ctx, "Body.")
        assert "# Test Slug" in result

    def test_empty_articles_still_produces_structure(self):
        ctx = WritingContext(
            articles=[],
            output_type="blog",
            steering_base_path="/tmp",
            screenshots_path="screenshots/",
            run_date=date(2025, 7, 14),
            slug="empty-run",
        )
        result = assemble_blog_post(ctx, "Generated body.")
        assert "<!-- MIKE:" in result
        assert "## References" in result
        assert "Generated body." in result


# ---------------------------------------------------------------------------
# build_youtube_prompt
# ---------------------------------------------------------------------------


class TestBuildYoutubePrompt:
    def test_includes_voice_rules(self):
        ctx = _make_context("youtube")
        steering = {"voice": "Be conversational.", "template": "YouTube template."}
        prompt = build_youtube_prompt(ctx, steering)
        assert "Be conversational." in prompt

    def test_includes_template(self):
        ctx = _make_context("youtube")
        steering = {"voice": "Voice.", "template": "YouTube structure here."}
        prompt = build_youtube_prompt(ctx, steering)
        assert "YouTube structure here." in prompt

    def test_includes_article_citations(self):
        ctx = _make_context("youtube")
        steering = {"voice": "Voice.", "template": "Template."}
        prompt = build_youtube_prompt(ctx, steering)
        assert "(Author, 2025)" in prompt
        assert "Test Article" in prompt

    def test_includes_four_section_instructions(self):
        ctx = _make_context("youtube")
        steering = {"voice": "Voice."}
        prompt = build_youtube_prompt(ctx, steering)
        assert "The Problem" in prompt
        assert "Architecture Walkthrough" in prompt
        assert "The Build" in prompt
        assert "Results + Cost" in prompt

    def test_includes_broll_cue_example(self):
        ctx = _make_context("youtube")
        steering = {"voice": "Voice."}
        prompt = build_youtube_prompt(ctx, steering)
        assert "B-ROLL" in prompt
        assert "screenshots/console-runtime.png" in prompt

    def test_includes_run_metadata(self):
        ctx = _make_context("youtube")
        steering = {"voice": "Voice."}
        prompt = build_youtube_prompt(ctx, steering)
        assert "2025-07-14" in prompt
        assert "test-slug" in prompt


# ---------------------------------------------------------------------------
# assemble_youtube_script
# ---------------------------------------------------------------------------


class TestAssembleYoutubeScript:
    def test_contains_thumbnail_concept(self):
        ctx = _make_context("youtube")
        result = assemble_youtube_script(ctx, "Body content.")
        assert "## Thumbnail Concept" in result
        assert "Visual:" in result

    def test_thumbnail_references_top_article(self):
        ctx = _make_context("youtube")
        result = assemble_youtube_script(ctx, "Body.")
        assert "Test Article" in result

    def test_contains_cold_open_placeholder(self):
        ctx = _make_context("youtube")
        result = assemble_youtube_script(ctx, "Body.")
        assert "<!-- MIKE: [Cold open, 30-45s to camera." in result
        assert "Topic suggestion: Test Article" in result
        assert "~no script] -->" in result

    def test_contains_generated_body(self):
        ctx = _make_context("youtube")
        body = "## The Problem\n\nSomething.\n\n## The Build\n\nStep one."
        result = assemble_youtube_script(ctx, body)
        assert "Something." in result
        assert "Step one." in result

    def test_contains_outro_placeholder(self):
        ctx = _make_context("youtube")
        result = assemble_youtube_script(ctx, "Body.")
        assert "<!-- MIKE: [Outro, ~30s to camera] -->" in result

    def test_empty_articles_still_produces_structure(self):
        ctx = WritingContext(
            articles=[],
            output_type="youtube",
            steering_base_path="/tmp",
            screenshots_path="screenshots/",
            run_date=date(2025, 7, 14),
            slug="empty-run",
        )
        result = assemble_youtube_script(ctx, "Generated body.")
        assert "<!-- MIKE:" in result
        assert "Generated body." in result
        assert "## Thumbnail Concept" in result


# ---------------------------------------------------------------------------
# assemble_youtube_description
# ---------------------------------------------------------------------------


class TestAssembleYoutubeDescription:
    def test_contains_required_hashtags(self):
        ctx = _make_context("youtube")
        result = assemble_youtube_description(ctx)
        for tag in ["#AWS", "#AWSCommunity", "#KiroIDE", "#AgentCore", "#BuildOnAWS", "#Aotearoa"]:
            assert tag in result

    def test_contains_article_titles(self):
        ctx = _make_context("youtube")
        result = assemble_youtube_description(ctx)
        assert "Test Article" in result

    def test_contains_article_links(self):
        ctx = _make_context("youtube")
        result = assemble_youtube_description(ctx)
        assert "https://example.com/post" in result

    def test_contains_niche_context(self):
        ctx = _make_context("youtube")
        result = assemble_youtube_description(ctx)
        assert "Aotearoa" in result
        assert "Kiro IDE" in result

    def test_empty_articles_still_produces_description(self):
        ctx = WritingContext(
            articles=[],
            output_type="youtube",
            steering_base_path="/tmp",
            screenshots_path="screenshots/",
            run_date=date(2025, 7, 14),
            slug="empty-run",
        )
        result = assemble_youtube_description(ctx)
        assert "#AWS" in result
        assert "#Aotearoa" in result


# ---------------------------------------------------------------------------
# build_cfp_prompt
# ---------------------------------------------------------------------------


class TestBuildCfpPrompt:
    def test_includes_voice_rules(self):
        ctx = _make_context("cfp")
        steering = {"voice": "Be conversational.", "template": "CFP template."}
        prompt = build_cfp_prompt(ctx, steering)
        assert "Be conversational." in prompt

    def test_includes_template(self):
        ctx = _make_context("cfp")
        steering = {"voice": "Voice.", "template": "CFP structure here."}
        prompt = build_cfp_prompt(ctx, steering)
        assert "CFP structure here." in prompt

    def test_includes_article_citations(self):
        ctx = _make_context("cfp")
        steering = {"voice": "Voice.", "template": "Template."}
        prompt = build_cfp_prompt(ctx, steering)
        assert "(Author, 2025)" in prompt
        assert "Test Article" in prompt

    def test_includes_cfp_specific_instructions(self):
        ctx = _make_context("cfp")
        steering = {"voice": "Voice."}
        prompt = build_cfp_prompt(ctx, steering)
        assert "Abstract body" in prompt
        assert "takeaways" in prompt.lower()
        assert "Target audience" in prompt

    def test_includes_run_metadata(self):
        ctx = _make_context("cfp")
        steering = {"voice": "Voice."}
        prompt = build_cfp_prompt(ctx, steering)
        assert "2025-07-14" in prompt
        assert "test-slug" in prompt

    def test_no_template_key_still_works(self):
        ctx = _make_context("cfp")
        steering = {"voice": "Voice only."}
        prompt = build_cfp_prompt(ctx, steering)
        assert "Voice only." in prompt
        assert "Output template" not in prompt


# ---------------------------------------------------------------------------
# assemble_cfp_proposal
# ---------------------------------------------------------------------------


class TestAssembleCfpProposal:
    def test_contains_three_title_options(self):
        ctx = _make_context("cfp")
        result = assemble_cfp_proposal(ctx, "Body content.")
        assert "## Title Options" in result
        assert "Technical:" in result
        assert "Community:" in result
        assert "Personal story:" in result

    def test_title_options_reference_top_article(self):
        ctx = _make_context("cfp")
        result = assemble_cfp_proposal(ctx, "Body.")
        assert "Test Article" in result

    def test_contains_generated_body(self):
        ctx = _make_context("cfp")
        body = "## Abstract\n\nThis talk covers AgentCore.\n\n## Takeaways\n\n1. First."
        result = assemble_cfp_proposal(ctx, body)
        assert "This talk covers AgentCore." in result
        assert "First." in result

    def test_contains_25_min_outline(self):
        ctx = _make_context("cfp")
        result = assemble_cfp_proposal(ctx, "Body.")
        assert "## Session Outline (25 minutes)" in result
        assert "Introduction" in result
        assert "Q&A" in result

    def test_contains_45_min_outline(self):
        ctx = _make_context("cfp")
        result = assemble_cfp_proposal(ctx, "Body.")
        assert "## Session Outline (45 minutes)" in result
        assert "Extended live demo" in result

    def test_speaker_bio_contains_required_details(self):
        ctx = _make_context("cfp")
        result = assemble_cfp_proposal(ctx, "Body.")
        assert "## Speaker Bio" in result
        assert "AWS Community Builder" in result
        assert "AI Engineering" in result
        assert "2026" in result
        assert "kiro-steering-docs-extension" in result
        assert "AWS User Group Oceania" in result
        assert "Palmerston North" in result
        assert "Aotearoa" in result

    def test_contains_personal_note_placeholder(self):
        ctx = _make_context("cfp")
        result = assemble_cfp_proposal(ctx, "Body.")
        assert "<!-- MIKE: [Personal note for this CFP, ~50 words] -->" in result

    def test_contains_all_events(self):
        ctx = _make_context("cfp")
        result = assemble_cfp_proposal(ctx, "Body.")
        assert "## Target Events" in result
        assert "AWS Summit Sydney" in result
        assert "AWS Summit Auckland" in result
        assert "AWS Community Day Oceania" in result
        assert "KiwiCon" in result
        assert "YOW!" in result
        assert "DevOpsDays NZ" in result
        assert "NDC Sydney" in result

    def test_events_list_has_all_six_events(self):
        ctx = _make_context("cfp")
        result = assemble_cfp_proposal(ctx, "Body.")
        events_section = result[result.index("## Target Events"):]
        event_lines = [line for line in events_section.split("\n") if line.startswith("- ")]
        assert len(event_lines) == 7  # Sydney and Auckland listed separately

    def test_empty_articles_still_produces_structure(self):
        ctx = WritingContext(
            articles=[],
            output_type="cfp",
            steering_base_path="/tmp",
            screenshots_path="screenshots/",
            run_date=date(2025, 7, 14),
            slug="empty-run",
        )
        result = assemble_cfp_proposal(ctx, "Generated body.")
        assert "## Title Options" in result
        assert "## Session Outline (25 minutes)" in result
        assert "## Session Outline (45 minutes)" in result
        assert "## Speaker Bio" in result
        assert "<!-- MIKE:" in result
        assert "## Target Events" in result
        assert "Generated body." in result


# ---------------------------------------------------------------------------
# build_usergroup_prompt
# ---------------------------------------------------------------------------


class TestBuildUsergroupPrompt:
    def test_includes_voice_rules(self):
        ctx = _make_context("usergroup")
        steering = {"voice": "Be conversational.", "template": "UG template."}
        prompt = build_usergroup_prompt(ctx, steering)
        assert "Be conversational." in prompt

    def test_includes_template(self):
        ctx = _make_context("usergroup")
        steering = {"voice": "Voice.", "template": "User group structure here."}
        prompt = build_usergroup_prompt(ctx, steering)
        assert "User group structure here." in prompt

    def test_includes_article_citations(self):
        ctx = _make_context("usergroup")
        steering = {"voice": "Voice.", "template": "Template."}
        prompt = build_usergroup_prompt(ctx, steering)
        assert "(Author, 2025)" in prompt
        assert "Test Article" in prompt

    def test_includes_usergroup_specific_instructions(self):
        ctx = _make_context("usergroup")
        steering = {"voice": "Voice."}
        prompt = build_usergroup_prompt(ctx, steering)
        assert "Session Outline" in prompt
        assert "Live Demo Instructions" in prompt
        assert "audience participation" in prompt

    def test_includes_run_metadata(self):
        ctx = _make_context("usergroup")
        steering = {"voice": "Voice."}
        prompt = build_usergroup_prompt(ctx, steering)
        assert "2025-07-14" in prompt
        assert "test-slug" in prompt

    def test_no_template_key_still_works(self):
        ctx = _make_context("usergroup")
        steering = {"voice": "Voice only."}
        prompt = build_usergroup_prompt(ctx, steering)
        assert "Voice only." in prompt
        assert "Output template" not in prompt


# ---------------------------------------------------------------------------
# assemble_usergroup_session
# ---------------------------------------------------------------------------


class TestAssembleUsergroupSession:
    def test_contains_recommended_format_section(self):
        ctx = _make_context("usergroup")
        result = assemble_usergroup_session(ctx, "Body content.")
        assert "## Recommended Format" in result

    def test_recommended_format_mentions_all_three_options(self):
        ctx = _make_context("usergroup")
        result = assemble_usergroup_session(ctx, "Body.")
        assert "Lightning" in result or "lightning" in result.lower()
        assert "10 minutes" in result
        assert "Standard" in result or "standard" in result.lower()
        assert "30 minutes" in result
        assert "Workshop" in result or "workshop" in result.lower()
        assert "60 minutes" in result

    def test_recommended_format_lightning_for_few_articles(self):
        ctx = WritingContext(
            articles=[_make_article_with_citation()],
            output_type="usergroup",
            steering_base_path="/tmp",
            screenshots_path="screenshots/",
            run_date=date(2025, 7, 14),
            slug="test-slug",
        )
        result = assemble_usergroup_session(ctx, "Body.")
        # With 1 article, should recommend lightning
        assert "**Lightning (10 minutes)**" in result

    def test_recommended_format_standard_for_moderate_articles(self):
        articles = [_make_article_with_citation() for _ in range(4)]
        ctx = WritingContext(
            articles=articles,
            output_type="usergroup",
            steering_base_path="/tmp",
            screenshots_path="screenshots/",
            run_date=date(2025, 7, 14),
            slug="test-slug",
        )
        result = assemble_usergroup_session(ctx, "Body.")
        assert "**Standard (30 minutes)**" in result

    def test_recommended_format_workshop_for_many_articles(self):
        articles = [_make_article_with_citation() for _ in range(7)]
        ctx = WritingContext(
            articles=articles,
            output_type="usergroup",
            steering_base_path="/tmp",
            screenshots_path="screenshots/",
            run_date=date(2025, 7, 14),
            slug="test-slug",
        )
        result = assemble_usergroup_session(ctx, "Body.")
        assert "**Workshop (60 minutes)**" in result

    def test_contains_opening_story_placeholder(self):
        ctx = _make_context("usergroup")
        result = assemble_usergroup_session(ctx, "Body.")
        assert "<!-- MIKE: [Opening story for this session, ~50 words] -->" in result

    def test_contains_generated_body(self):
        ctx = _make_context("usergroup")
        body = "## Session Outline\n\nExplanation segment.\n\n## Live Demo\n\nStep one."
        result = assemble_usergroup_session(ctx, body)
        assert "Explanation segment." in result
        assert "Step one." in result

    def test_contains_slide_outline(self):
        ctx = _make_context("usergroup")
        result = assemble_usergroup_session(ctx, "Body.")
        assert "## Slide Outline" in result

    def test_slide_outline_max_12_slides(self):
        # Create many articles to test the cap
        articles = [_make_article_with_citation() for _ in range(15)]
        ctx = WritingContext(
            articles=articles,
            output_type="usergroup",
            steering_base_path="/tmp",
            screenshots_path="screenshots/",
            run_date=date(2025, 7, 14),
            slug="test-slug",
        )
        result = assemble_usergroup_session(ctx, "Body.")
        slide_section = result[result.index("## Slide Outline"):]
        slide_lines = [line for line in slide_section.split("\n") if line and line[0].isdigit()]
        assert len(slide_lines) <= 12

    def test_slide_outline_is_numbered_list(self):
        ctx = _make_context("usergroup")
        result = assemble_usergroup_session(ctx, "Body.")
        slide_section = result[result.index("## Slide Outline"):]
        slide_lines = [line for line in slide_section.split("\n") if line and line[0].isdigit()]
        assert len(slide_lines) >= 1
        assert slide_lines[0].startswith("1.")

    def test_contains_title(self):
        ctx = _make_context("usergroup")
        result = assemble_usergroup_session(ctx, "Body.")
        assert "# User Group Session: Test Slug" in result

    def test_empty_articles_still_produces_structure(self):
        ctx = WritingContext(
            articles=[],
            output_type="usergroup",
            steering_base_path="/tmp",
            screenshots_path="screenshots/",
            run_date=date(2025, 7, 14),
            slug="empty-run",
        )
        result = assemble_usergroup_session(ctx, "Generated body.")
        assert "## Recommended Format" in result
        assert "<!-- MIKE:" in result
        assert "## Slide Outline" in result
        assert "Generated body." in result


# ---------------------------------------------------------------------------
# build_digest_prompt
# ---------------------------------------------------------------------------


class TestBuildDigestPrompt:
    def test_includes_voice_rules(self):
        ctx = _make_context("digest")
        steering = {"voice": "Be conversational."}
        prompt = build_digest_prompt(ctx, steering)
        assert "Be conversational." in prompt

    def test_includes_articles(self):
        ctx = _make_context("digest")
        steering = {"voice": "Voice."}
        prompt = build_digest_prompt(ctx, steering)
        assert "Test Article" in prompt
        assert "https://example.com/post" in prompt

    def test_includes_digest_specific_instructions(self):
        ctx = _make_context("digest")
        steering = {"voice": "Voice."}
        prompt = build_digest_prompt(ctx, steering)
        assert "3-4 sentences" in prompt
        assert "plain English" in prompt

    def test_mentions_grouping_for_five_plus_articles(self):
        articles = [_make_article_with_citation() for _ in range(5)]
        ctx = WritingContext(
            articles=articles,
            output_type="digest",
            steering_base_path="/tmp",
            screenshots_path="screenshots/",
            run_date=date(2025, 7, 14),
            slug="test-slug",
        )
        steering = {"voice": "Voice."}
        prompt = build_digest_prompt(ctx, steering)
        assert "group" in prompt.lower()
        assert "theme" in prompt.lower()

    def test_no_grouping_instruction_for_fewer_than_five(self):
        ctx = _make_context("digest")  # 1 article
        steering = {"voice": "Voice."}
        prompt = build_digest_prompt(ctx, steering)
        assert "Group them by theme" not in prompt

    def test_includes_run_metadata(self):
        ctx = _make_context("digest")
        steering = {"voice": "Voice."}
        prompt = build_digest_prompt(ctx, steering)
        assert "2025-07-14" in prompt
        assert "test-slug" in prompt


# ---------------------------------------------------------------------------
# assemble_digest_email
# ---------------------------------------------------------------------------


class TestAssembleDigestEmail:
    def test_contains_personal_note_placeholder(self):
        ctx = _make_context("digest")
        result = assemble_digest_email(ctx, "Body content.")
        assert "<!-- MIKE: [Personal note for this digest, 2-3 sentences] -->" in result

    def test_placeholder_uses_correct_format(self):
        ctx = _make_context("digest")
        result = assemble_digest_email(ctx, "Body.")
        assert result.startswith("<!-- MIKE:")
        assert "-->" in result

    def test_contains_generated_body(self):
        ctx = _make_context("digest")
        body = "AgentCore Browser launched this week. It matters for testing."
        result = assemble_digest_email(ctx, body)
        assert "AgentCore Browser launched this week." in result

    def test_contains_sign_off(self):
        ctx = _make_context("digest")
        result = assemble_digest_email(ctx, "Body.")
        assert "Mike" in result

    def test_handles_empty_articles_gracefully(self):
        ctx = WritingContext(
            articles=[],
            output_type="digest",
            steering_base_path="/tmp",
            screenshots_path="screenshots/",
            run_date=date(2025, 7, 14),
            slug="empty-run",
        )
        result = assemble_digest_email(ctx, "No articles this week.")
        assert "<!-- MIKE:" in result
        assert "No articles this week." in result

    def test_body_whitespace_is_stripped(self):
        ctx = _make_context("digest")
        result = assemble_digest_email(ctx, "  Body with spaces.  \n\n")
        assert "Body with spaces." in result
        # Should not have leading/trailing whitespace around body
        assert "  Body" not in result
