"""Writer Lambda — ContentBrief to output bundle.

Receives a ContentBrief (and optional revision_feedback), reads steering
files at runtime via load_steering(), generates all requested content
files, and returns a WriterManifest.

Voice rules enforced on every output:
- No banned phrases: leverage, empower, unlock, dive into, game-changer
- No em-dashes (U+2014 or &#8212;)
- No paragraph or section opening with "I"
- Proper <!-- MIKE: --> placeholder format

Model routing:
- Claude Sonnet: blog post, YouTube script, CFP proposal, user group session
- Claude Haiku: digest email

Requirements: REQ-007.1–REQ-007.5, REQ-008.1–REQ-008.3, REQ-021.3,
             REQ-026.1
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Protocol

from magic_content_engine.bullpen.models import (
    ContentBrief,
    FileEntry,
    ScoredArticle,
    WriterInput,
    WriterManifest,
)
from magic_content_engine.config import HAIKU_MODEL_ID, SONNET_MODEL_ID
from magic_content_engine.steering import load_steering
from magic_content_engine.writing_agent import (
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
    validate_voice_rules,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM protocol — testable seam
# ---------------------------------------------------------------------------


class LLMProtocol(Protocol):
    """Protocol for LLM generation calls."""

    def __call__(self, *, model_id: str, prompt: str) -> str:
        """Generate text from *prompt* using the given *model_id*."""
        ...


# ---------------------------------------------------------------------------
# Output type → filename mapping
# ---------------------------------------------------------------------------

OUTPUT_TYPE_TO_FILENAME: dict[str, str] = {
    "blog": "post.md",
    "youtube_script": "script.md",
    "youtube_description": "description.txt",
    "cfp": "cfp-proposal.md",
    "usergroup": "usergroup-session.md",
    "digest": "digest-email.txt",
}

# Output types that use Sonnet vs Haiku
_SONNET_TYPES = {"blog", "youtube", "cfp", "usergroup"}
_HAIKU_TYPES = {"digest"}


def _model_for(output_type: str) -> str:
    """Return the Bedrock model ID for the given output type."""
    if output_type in _HAIKU_TYPES:
        return HAIKU_MODEL_ID
    return SONNET_MODEL_ID


# ---------------------------------------------------------------------------
# Revision feedback injection
# ---------------------------------------------------------------------------

_REVISION_HEADER = "\n\n## Revision feedback from Subeditor\n"


def _inject_revision_feedback(prompt: str, feedback: str) -> str:
    """Append Subeditor revision feedback to an existing prompt."""
    return prompt + _REVISION_HEADER + feedback.strip()


# ---------------------------------------------------------------------------
# Word count helper
# ---------------------------------------------------------------------------


def _count_words(text: str) -> int:
    """Count words in *text*, excluding HTML comment blocks."""
    # Strip <!-- ... --> comment blocks before counting
    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return len(stripped.split())


# ---------------------------------------------------------------------------
# Core generation helpers
# ---------------------------------------------------------------------------


def _strip_llm_artifacts(body: str) -> str:
    """Remove artifacts the LLM sometimes adds despite being told not to.

    Strips:
    - MIKE placeholder comments (added by assembler, not LLM)
    - Markdown code fences wrapping the entire body
    - Trailing References sections (added by assembler)
    - Em-dashes (replace with plain hyphen)
    """
    import re

    # Remove any <!-- MIKE: ... --> blocks (single or multiline)
    body = re.sub(r'<!--\s*MIKE:.*?-->', '', body, flags=re.DOTALL)

    # Remove opening/closing markdown code fences that wrap the whole body
    # Pattern: ```markdown or ``` at start of line
    body = re.sub(r'^```[a-z]*\s*\n', '', body, flags=re.MULTILINE)
    body = re.sub(r'\n```\s*$', '', body, flags=re.MULTILINE)

    # Remove any trailing ## References section (assembler adds its own)
    body = re.sub(r'\n## References\b.*$', '', body, flags=re.DOTALL)

    # Replace em-dashes with plain hyphens
    body = body.replace('\u2014', ' - ').replace('&#8212;', ' - ')

    # Clean up excessive blank lines left by removals
    body = re.sub(r'\n{3,}', '\n\n', body)

    return body.strip()


def _generate_blog(
    context: WritingContext,
    steering: dict[str, str],
    llm: LLMProtocol,
    revision_feedback: str | None,
) -> str:
    """Generate post.md content."""
    prompt = build_blog_prompt(context, steering)
    if revision_feedback:
        prompt = _inject_revision_feedback(prompt, revision_feedback)
    body = llm(model_id=_model_for("blog"), prompt=prompt)
    body = _strip_llm_artifacts(body)
    return assemble_blog_post(context, body)


def _generate_youtube(
    context: WritingContext,
    steering: dict[str, str],
    llm: LLMProtocol,
    revision_feedback: str | None,
) -> tuple[str, str]:
    """Generate (script.md, description.txt) content."""
    prompt = build_youtube_prompt(context, steering)
    if revision_feedback:
        prompt = _inject_revision_feedback(prompt, revision_feedback)
    body = llm(model_id=_model_for("youtube"), prompt=prompt)
    body = _strip_llm_artifacts(body)
    script = assemble_youtube_script(context, body)
    description = assemble_youtube_description(context)
    return script, description


def _generate_cfp(
    context: WritingContext,
    steering: dict[str, str],
    llm: LLMProtocol,
    revision_feedback: str | None,
) -> str:
    """Generate cfp-proposal.md content."""
    prompt = build_cfp_prompt(context, steering)
    if revision_feedback:
        prompt = _inject_revision_feedback(prompt, revision_feedback)
    body = llm(model_id=_model_for("cfp"), prompt=prompt)
    body = _strip_llm_artifacts(body)
    return assemble_cfp_proposal(context, body)


def _generate_usergroup(
    context: WritingContext,
    steering: dict[str, str],
    llm: LLMProtocol,
    revision_feedback: str | None,
) -> str:
    """Generate usergroup-session.md content."""
    prompt = build_usergroup_prompt(context, steering)
    if revision_feedback:
        prompt = _inject_revision_feedback(prompt, revision_feedback)
    body = llm(model_id=_model_for("usergroup"), prompt=prompt)
    body = _strip_llm_artifacts(body)
    return assemble_usergroup_session(context, body)


def _generate_digest(
    context: WritingContext,
    steering: dict[str, str],
    llm: LLMProtocol,
    revision_feedback: str | None,
) -> str:
    """Generate digest-email.txt content."""
    prompt = build_digest_prompt(context, steering)
    if revision_feedback:
        prompt = _inject_revision_feedback(prompt, revision_feedback)
    body = llm(model_id=_model_for("digest"), prompt=prompt)
    return assemble_digest_email(context, body)


# ---------------------------------------------------------------------------
# WritingContext builder from ContentBrief
# ---------------------------------------------------------------------------


def _build_writing_context(
    brief: ContentBrief,
    output_type: str,
    steering_base_path: str,
) -> WritingContext:
    """Convert a ContentBrief into a WritingContext for the given output type."""
    from datetime import date as _date
    from magic_content_engine.models import (
        APACitation,
        Article,
        ArticleMetadata,
    )

    run_date = (
        _date.fromisoformat(brief.run_date)
        if brief.run_date
        else _date.today()
    )

    articles_with_citations: list[ArticleWithCitation] = []
    for sa in brief.selected_articles:
        article = Article(
            url=sa.url,
            title=sa.title,
            source=sa.source,
            source_type="primary",
            discovered_date=run_date,
            relevance_score=sa.relevance_score,
            status="confirmed",
            body=sa.summary,
        )
        metadata = ArticleMetadata(
            article_url=sa.url,
            title=sa.title,
            author="Amazon Web Services",
            publisher=sa.source,
        )
        # Build a minimal APA citation from available data
        year = run_date.year
        citation = APACitation(
            metadata=metadata,
            reference_entry=(
                f"Amazon Web Services. ({year}). {sa.title}. {sa.source}. {sa.url}"
            ),
            in_text_citation=f"(Amazon Web Services, {year})",
            bibtex_entry=(
                f"@online{{aws{year},\n"
                f"  title = {{{sa.title}}},\n"
                f"  url = {{{sa.url}}},\n"
                f"  year = {{{year}}}\n}}"
            ),
        )
        articles_with_citations.append(
            ArticleWithCitation(article=article, citation=citation)
        )

    slug = brief.slug or "weekly-update"

    return WritingContext(
        articles=articles_with_citations,
        output_type=output_type,
        steering_base_path=steering_base_path,
        screenshots_path="screenshots/",
        run_date=run_date,
        slug=slug,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_writer(
    writer_input: WriterInput,
    llm: LLMProtocol,
) -> WriterManifest:
    """Execute the Writer Lambda: ContentBrief → output bundle.

    For each requested output type in the ContentBrief:
    1. Load steering files via load_steering().
    2. Build a WritingContext from the ContentBrief.
    3. Generate content using the appropriate prompt builder + assembler.
    4. Inject revision_feedback into the prompt when present.
    5. Validate voice rules (log warnings; never block output).
    6. Write the file to output_dir/YYYY-MM-DD-[slug]/.
    7. Record the FileEntry in the manifest.

    Returns a WriterManifest with all files written, voice_rules_applied=True,
    and a run_timestamp.

    On per-output-type failure, logs the error and continues with remaining
    types (log-and-continue per REQ-025.3).
    """
    brief = writer_input.content_brief
    steering_base_path = writer_input.steering_base_path
    output_dir = writer_input.output_dir
    revision_feedback = writer_input.revision_feedback

    from datetime import date as _date

    run_date = (
        _date.fromisoformat(brief.run_date) if brief.run_date else _date.today()
    )
    slug = brief.slug or "weekly-update"
    bundle_dir_name = f"{run_date.isoformat()}-{slug}"
    bundle_path = pathlib.Path(output_dir) / bundle_dir_name
    bundle_path.mkdir(parents=True, exist_ok=True)

    files_written: list[FileEntry] = []

    for output_type in brief.output_types:
        try:
            _write_output_type(
                output_type=output_type,
                brief=brief,
                steering_base_path=steering_base_path,
                bundle_path=bundle_path,
                llm=llm,
                revision_feedback=revision_feedback,
                files_written=files_written,
            )
        except Exception as exc:
            logger.error(
                "Writer failed for output_type=%s: %s",
                output_type,
                exc,
                exc_info=True,
            )
            # log-and-continue: skip this output type, proceed with others

    run_timestamp = datetime.now(tz=timezone.utc).isoformat()

    return WriterManifest(
        files_written=files_written,
        voice_rules_applied=True,
        run_timestamp=run_timestamp,
    )


def _write_output_type(
    output_type: str,
    brief: ContentBrief,
    steering_base_path: str,
    bundle_path: pathlib.Path,
    llm: LLMProtocol,
    revision_feedback: str | None,
    files_written: list[FileEntry],
) -> None:
    """Generate and write files for a single output type.

    Handles the youtube type specially (produces two files: script.md +
    description.txt). All other types produce a single file.
    """
    steering = load_steering(steering_base_path, output_type)
    context = _build_writing_context(brief, output_type, steering_base_path)

    if output_type == "blog":
        content = _generate_blog(context, steering, llm, revision_feedback)
        _write_file(bundle_path, "post.md", "blog", content, files_written)

    elif output_type == "youtube":
        script, description = _generate_youtube(
            context, steering, llm, revision_feedback
        )
        _write_file(bundle_path, "script.md", "youtube", script, files_written)
        _write_file(
            bundle_path, "description.txt", "youtube", description, files_written
        )

    elif output_type == "cfp":
        content = _generate_cfp(context, steering, llm, revision_feedback)
        _write_file(bundle_path, "cfp-proposal.md", "cfp", content, files_written)

    elif output_type == "usergroup":
        content = _generate_usergroup(context, steering, llm, revision_feedback)
        _write_file(
            bundle_path, "usergroup-session.md", "usergroup", content, files_written
        )

    elif output_type == "digest":
        content = _generate_digest(context, steering, llm, revision_feedback)
        _write_file(
            bundle_path, "digest-email.txt", "digest", content, files_written
        )

    else:
        raise ValueError(f"Unknown output type: {output_type!r}")


def _write_file(
    bundle_path: pathlib.Path,
    filename: str,
    output_type: str,
    content: str,
    files_written: list[FileEntry],
) -> None:
    """Write *content* to *bundle_path/filename*, validate voice rules, record entry."""
    # Final safety net: scrub em-dashes and any stray Unicode dashes from the
    # fully assembled content. Em-dashes can enter via article titles embedded
    # in MIKE placeholder instructions (e.g. "Strands Agents\u2014Open Source..."
    # from a scraped source), even when the LLM output itself is clean.
    content = (
        content
        .replace("\u2014", " - ")  # em-dash
        .replace("\u2013", " - ")  # en-dash
        .replace("&#8212;", " - ")
        .replace("&mdash;", " - ")
    )

    file_path = bundle_path / filename
    file_path.write_text(content, encoding="utf-8")

    violations = validate_voice_rules(content)
    if violations:
        logger.warning(
            "Voice-rule violations in %s (%s): %s",
            filename,
            output_type,
            "; ".join(violations),
        )

    relative_path = str(pathlib.Path(bundle_path.name) / filename)
    word_count = _count_words(content)

    files_written.append(
        FileEntry(
            path=relative_path,
            output_type=output_type,
            word_count=word_count,
        )
    )
    logger.info("Wrote %s (%d words)", relative_path, word_count)
