"""Orchestrator_Agent — end-to-end workflow for the Magic Content Engine.

Wires all components in sequence: trigger acceptance, research crawl,
deduplication, scoring, metadata extraction, citation building, user
interaction, content generation, screenshot capture, bundle assembly,
Publish Gate review, S3 upload, and terminal summary.

Uses protocol-based dependency injection for all external services
so the orchestrator is fully testable.

Requirements: REQ-001.1, REQ-001.2, REQ-001.3, REQ-019.1, REQ-019.3
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Protocol, runtime_checkable

from magic_content_engine.config import LOG_LEVEL, S3_BUCKET, S3_KEY_PREFIX, STEERING_BASE_PATH
from magic_content_engine.errors import ErrorCollector, StepError
from magic_content_engine.models import (
    AgentLog,
    Article,
    CostEstimate,
    ModelInvocation,
    OutputBundle,
)


# ---------------------------------------------------------------------------
# Protocols for external services
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryProtocol(Protocol):
    """Long-term memory for voice profile and covered URLs."""

    def load_voice_profile(self) -> str:
        """Load the voice profile text from long-term memory."""
        ...

    def load_covered_urls(self) -> set[str]:
        """Load previously covered article URLs."""
        ...

    def store_covered_urls(self, urls: set[str], run_date: date) -> None:
        """Persist confirmed article URLs."""
        ...


# ---------------------------------------------------------------------------
# WorkflowDependencies — all injected protocols
# ---------------------------------------------------------------------------


@dataclass
class WorkflowDependencies:
    """All injected dependencies for the orchestrator workflow.

    Each field corresponds to a protocol used by one or more workflow
    steps. Tests inject lightweight stubs; production wires real
    AWS-backed implementations.
    """

    # Long-term memory (voice profile, covered URLs)
    memory: MemoryProtocol

    # Deduplication memory (MemoryProtocol from deduplication.py)
    dedup_memory: "deduplication.MemoryProtocol"  # type: ignore[name-defined]

    # Topic coverage memory
    topic_memory: "topic_coverage.TopicCoverageMemoryProtocol"  # type: ignore[name-defined]

    # Engagement memory + API
    engagement_api: "engagement.DevToAPIProtocol"  # type: ignore[name-defined]
    engagement_memory: "engagement.EngagementMemoryProtocol"  # type: ignore[name-defined]

    # Embargo / held items
    held_item_memory: "embargo.HeldItemMemoryProtocol"  # type: ignore[name-defined]
    ses_notifier: "embargo.SESNotifierProtocol"  # type: ignore[name-defined]

    # Browser for crawling and screenshots
    browser: "crawler.BrowserProtocol"  # type: ignore[name-defined]

    # LLM callables (scoring, metadata, citation, writing)
    llm_scorer: "scoring.LLMScorer"  # type: ignore[name-defined]
    llm_extractor: "metadata.LLMExtractor"  # type: ignore[name-defined]
    llm_formatter: "citation.LLMFormatter"  # type: ignore[name-defined]
    llm_writer: "writing_agent.LLMProtocol"  # type: ignore[name-defined]

    # Screenshot browser (may be same as crawler browser)
    screenshot_browser: "screenshots.BrowserProtocol"  # type: ignore[name-defined]

    # S3 client
    s3_client: "s3_upload.S3ClientProtocol"  # type: ignore[name-defined]

    # File operations for bundle assembly
    bundle_file_ops: "bundle.FileOps"  # type: ignore[name-defined]

    # Publish Gate file operations
    gate_file_ops: Optional["publish_gate.FileOps"] = None  # type: ignore[name-defined]

    # Input function override (for testing)
    input_fn: "Callable[[str], str]" = input  # type: ignore[name-defined]

    # S3 bucket override
    s3_bucket: str = S3_BUCKET

    # Steering base path override
    steering_base_path: str = STEERING_BASE_PATH

    # Unattended mode
    unattended: bool = False


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Workflow execution
# ---------------------------------------------------------------------------


def run_workflow(
    deps: WorkflowDependencies,
    source: str,
    run_date: date,
) -> AgentLog:
    """Execute the full 20-step orchestrator workflow.

    Uses ErrorCollector throughout for log-and-continue semantics.
    Returns a populated AgentLog at the end.
    """
    from magic_content_engine.bundle import assemble_bundle, format_cost_estimate
    from magic_content_engine.citation import aggregate_bibtex, build_citations
    from magic_content_engine.crawler import crawl_primary_sources, crawl_secondary_sources
    from magic_content_engine.deduplication import deduplicate_articles, store_confirmed_articles
    from magic_content_engine.embargo import check_embargo_releases
    from magic_content_engine.engagement import fetch_engagement_flow
    from magic_content_engine.metadata import extract_metadata
    from magic_content_engine.publish_gate import PublishGateDecision, run_publish_gate
    from magic_content_engine.s3_upload import upload_approved_files
    from magic_content_engine.scoring import score_articles
    from magic_content_engine.screenshots import capture_all_screenshots
    from magic_content_engine.slug import derive_topic, generate_slug, make_output_dirname
    from magic_content_engine.topic_coverage import (
        load_or_create_coverage_map,
        save_coverage_map,
        update_coverage_map,
    )
    from magic_content_engine.user_interaction import present_scored_articles, prompt_output_choice
    from magic_content_engine.weekly_brief import generate_weekly_brief, prompt_user_focus
    from magic_content_engine.writing_agent import ArticleWithCitation, WritingContext, generate_content

    collector = ErrorCollector()
    step_log: list[str] = []

    # Tracking variables
    all_articles: list[Article] = []
    confirmed_articles: list[Article] = []
    selected_outputs: list[str] = []
    generated_files: dict[str, str] = {}  # filename -> content
    screenshot_results: list[dict] = []
    uploaded_keys: list[str] = []

    def _log_step(name: str) -> None:
        step_log.append(name)
        logger.info("Step: %s", name)

    # ------------------------------------------------------------------
    # Step 1: Accept trigger
    # ------------------------------------------------------------------
    _log_step("accept_trigger")
    logger.info("Workflow started — source=%s, run_date=%s", source, run_date)

    # ------------------------------------------------------------------
    # Step 2: Load voice profile + previously covered URLs
    # ------------------------------------------------------------------
    _log_step("load_memory")
    try:
        voice_profile = deps.memory.load_voice_profile()
        covered_urls = deps.memory.load_covered_urls()
        logger.info("Loaded voice profile and %d covered URLs", len(covered_urls))
    except Exception as exc:
        collector.add(StepError(step="load_memory", target="memory", error_message=str(exc)))
        voice_profile = ""
        covered_urls = set()

    # ------------------------------------------------------------------
    # Step 3: Fetch engagement metrics (REQ-034)
    # ------------------------------------------------------------------
    _log_step("fetch_engagement")
    try:
        engagements = fetch_engagement_flow(
            api=deps.engagement_api,
            memory=deps.engagement_memory,
            collector=collector,
            run_date=run_date,
        )
    except Exception as exc:
        collector.add(StepError(step="fetch_engagement", target="dev.to", error_message=str(exc)))
        engagements = []

    # ------------------------------------------------------------------
    # Step 4: Load Topic_Coverage_Map (REQ-033)
    # ------------------------------------------------------------------
    _log_step("load_topic_coverage")
    try:
        coverage_map = load_or_create_coverage_map(deps.topic_memory, run_date)
    except Exception as exc:
        collector.add(StepError(step="load_topic_coverage", target="memory", error_message=str(exc)))
        from magic_content_engine.topic_coverage import create_empty_coverage_map
        coverage_map = create_empty_coverage_map(run_date)

    # ------------------------------------------------------------------
    # Step 5: Generate and present Weekly_Brief (REQ-035)
    # ------------------------------------------------------------------
    _log_step("weekly_brief")
    try:
        brief = generate_weekly_brief(coverage_map, engagements, run_date)
        brief = prompt_user_focus(brief)
    except Exception as exc:
        collector.add(StepError(step="weekly_brief", target="brief", error_message=str(exc)))
        brief = None

    # ------------------------------------------------------------------
    # Step 6: Check for released HeldItems (REQ-031, REQ-032)
    # ------------------------------------------------------------------
    _log_step("check_embargo")
    try:
        released_items = check_embargo_releases(
            memory=deps.held_item_memory,
            ses=deps.ses_notifier,
            collector=collector,
            run_date=run_date,
            input_fn=deps.input_fn,
        )
    except Exception as exc:
        collector.add(StepError(step="check_embargo", target="embargo", error_message=str(exc)))
        released_items = []

    # ------------------------------------------------------------------
    # Step 7: Crawl primary (5) and secondary (4) sources
    # ------------------------------------------------------------------
    _log_step("crawl_sources")
    try:
        primary = crawl_primary_sources(deps.browser, run_date, collector)
    except Exception as exc:
        collector.add(StepError(step="crawl", target="primary", error_message=str(exc)))
        primary = []

    try:
        secondary = crawl_secondary_sources(deps.browser, run_date, collector)
    except Exception as exc:
        collector.add(StepError(step="crawl", target="secondary", error_message=str(exc)))
        secondary = []

    all_articles = primary + secondary

    # ------------------------------------------------------------------
    # Step 8: Deduplicate against long-term memory
    # ------------------------------------------------------------------
    _log_step("deduplicate")
    try:
        new_articles = deduplicate_articles(all_articles, deps.dedup_memory)
    except Exception as exc:
        collector.add(StepError(step="deduplicate", target="dedup", error_message=str(exc)))
        new_articles = all_articles

    # ------------------------------------------------------------------
    # Step 9: Score articles via Haiku, exclude below threshold
    # ------------------------------------------------------------------
    _log_step("score_articles")
    try:
        scored = score_articles(
            new_articles,
            deps.llm_scorer,
            collector=collector,
            engagement_metrics=engagements if engagements else None,
        )
    except Exception as exc:
        collector.add(StepError(step="score", target="scoring", error_message=str(exc)))
        scored = []

    # ------------------------------------------------------------------
    # Step 10: Extract metadata and build APA citations
    # ------------------------------------------------------------------
    _log_step("extract_metadata")
    try:
        metadata_list = extract_metadata(scored, deps.llm_extractor, collector)
    except Exception as exc:
        collector.add(StepError(step="extract", target="metadata", error_message=str(exc)))
        metadata_list = []

    _log_step("build_citations")
    try:
        citations = build_citations(metadata_list, deps.llm_formatter, collector)
    except Exception as exc:
        collector.add(StepError(step="cite", target="citations", error_message=str(exc)))
        citations = []

    # ------------------------------------------------------------------
    # Step 11: Present scored articles, wait for user confirmation
    # ------------------------------------------------------------------
    _log_step("present_articles")
    try:
        confirmed_articles, removed = present_scored_articles(scored, deps.input_fn)
        for a in confirmed_articles:
            a.status = "confirmed"
    except Exception as exc:
        collector.add(StepError(step="present", target="articles", error_message=str(exc)))
        confirmed_articles = scored

    # ------------------------------------------------------------------
    # Step 12: Present output choice prompt
    # ------------------------------------------------------------------
    _log_step("output_choice")
    try:
        selected_outputs = prompt_output_choice(
            input_fn=deps.input_fn,
            unattended=deps.unattended,
        )
    except Exception as exc:
        collector.add(StepError(step="output_choice", target="prompt", error_message=str(exc)))
        selected_outputs = ["blog"]

    # ------------------------------------------------------------------
    # Step 13: Invoke Writing_Sub_Agent per selected output
    # ------------------------------------------------------------------
    _log_step("generate_content")
    # Build ArticleWithCitation pairs
    article_citation_pairs: list[ArticleWithCitation] = []
    citation_by_url = {c.metadata.article_url: c for c in citations}
    for article in confirmed_articles:
        cit = citation_by_url.get(article.url)
        if cit:
            article_citation_pairs.append(ArticleWithCitation(article=article, citation=cit))

    topic = derive_topic(confirmed_articles)
    slug = generate_slug(topic)
    dir_name = make_output_dirname(run_date, slug)
    screenshots_path = f"output/{dir_name}/screenshots"
    s3_key_prefix = f"{S3_KEY_PREFIX}{dir_name}/"

    for output_type in selected_outputs:
        ctx = WritingContext(
            articles=article_citation_pairs,
            output_type=output_type,
            steering_base_path=deps.steering_base_path,
            screenshots_path=screenshots_path,
            run_date=run_date,
            slug=slug,
        )
        try:
            content = generate_content(ctx, deps.llm_writer, collector)
            if content:
                filename = _output_type_to_filename(output_type)
                generated_files[filename] = content
        except Exception as exc:
            collector.add(StepError(step="generate", target=output_type, error_message=str(exc)))

    # ------------------------------------------------------------------
    # Step 14: Update Topic_Coverage_Map
    # ------------------------------------------------------------------
    _log_step("update_topic_coverage")
    try:
        topics_covered = list({a.title for a in confirmed_articles if a.title})
        article_titles = [a.title for a in confirmed_articles if a.title]
        update_coverage_map(coverage_map, topics_covered, article_titles, run_date)
        save_coverage_map(coverage_map, deps.topic_memory)
    except Exception as exc:
        collector.add(StepError(step="update_topic_coverage", target="coverage", error_message=str(exc)))

    # ------------------------------------------------------------------
    # Step 15: Capture screenshots
    # ------------------------------------------------------------------
    _log_step("capture_screenshots")
    try:
        captures = capture_all_screenshots(
            deps.screenshot_browser, confirmed_articles, screenshots_path, run_date, collector,
        )
        screenshot_results = [
            {"filename": c.filename, "success": c.success, "error": c.error}
            for c in captures
        ]
    except Exception as exc:
        collector.add(StepError(step="screenshot", target="all", error_message=str(exc)))

    # ------------------------------------------------------------------
    # Step 16: Assemble output bundle
    # ------------------------------------------------------------------
    _log_step("assemble_bundle")
    references_bib = aggregate_bibtex(citations)
    cost_estimate = CostEstimate(
        invocations=[], total_llm_cost_usd=0.0, total_agentcore_cost_usd=0.0, total_cost_usd=0.0,
    )
    agent_log = AgentLog(
        run_date=run_date.isoformat(),
        invocation_source=source,
        articles_found=len(all_articles),
        articles_kept=len(confirmed_articles),
        articles=[
            {"url": a.url, "score": a.relevance_score, "status": a.status}
            for a in all_articles
        ],
        model_usage=[],
        screenshot_results=screenshot_results,
        errors=collector.to_list(),
        selected_outputs=selected_outputs,
        run_metadata={"steps": step_log},
    )
    bundle = OutputBundle(
        run_date=run_date,
        slug=slug,
        selected_outputs=selected_outputs,
        generated_files=list(generated_files.keys()),
        references_bib=references_bib,
        cost_estimate=cost_estimate,
        agent_log=agent_log,
        s3_key_prefix=s3_key_prefix,
    )
    try:
        assemble_bundle(bundle, generated_files, deps.bundle_file_ops)
    except Exception as exc:
        collector.add(StepError(step="assemble", target="bundle", error_message=str(exc)))

    # ------------------------------------------------------------------
    # Step 17: Run Publish_Gate review
    # ------------------------------------------------------------------
    _log_step("publish_gate")
    approved_files: list[str] = []
    try:
        gate_results = run_publish_gate(
            outputs=generated_files,
            slug=slug,
            run_date=run_date,
            unattended=deps.unattended,
            bundle_dir=f"output/{dir_name}",
            s3_key_prefix=s3_key_prefix,
            article_titles=[a.title for a in confirmed_articles],
            file_ops=deps.gate_file_ops,
            input_fn=deps.input_fn,
        )
        approved_files = [
            f"output/{dir_name}/{r.filename}"
            for r in gate_results
            if r.decision == PublishGateDecision.APPROVE
        ]
    except Exception as exc:
        collector.add(StepError(step="publish_gate", target="gate", error_message=str(exc)))

    # ------------------------------------------------------------------
    # Step 18: Upload approved files to S3
    # ------------------------------------------------------------------
    _log_step("s3_upload")
    try:
        uploaded_keys = upload_approved_files(
            client=deps.s3_client,
            approved_files=approved_files,
            bucket=deps.s3_bucket,
            s3_key_prefix=s3_key_prefix,
            collector=collector,
        )
    except Exception as exc:
        collector.add(StepError(step="upload", target="s3", error_message=str(exc)))

    # ------------------------------------------------------------------
    # Step 19: Store confirmed article URLs in long-term memory
    # ------------------------------------------------------------------
    _log_step("store_urls")
    try:
        store_confirmed_articles(confirmed_articles, run_date, deps.dedup_memory)
    except Exception as exc:
        collector.add(StepError(step="store_urls", target="memory", error_message=str(exc)))

    # ------------------------------------------------------------------
    # Step 20: Print terminal summary
    # ------------------------------------------------------------------
    _log_step("terminal_summary")
    _print_terminal_summary(
        all_articles=all_articles,
        confirmed_articles=confirmed_articles,
        selected_outputs=selected_outputs,
        cost_estimate=cost_estimate,
        screenshot_results=screenshot_results,
        collector=collector,
        uploaded_keys=uploaded_keys,
    )

    # Update agent_log errors with final state
    agent_log.errors = collector.to_list()
    return agent_log


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OUTPUT_FILENAMES: dict[str, str] = {
    "blog": "post.md",
    "youtube": "script.md",
    "cfp": "cfp-proposal.md",
    "usergroup": "usergroup-session.md",
    "digest": "digest-email.txt",
}


def _output_type_to_filename(output_type: str) -> str:
    return _OUTPUT_FILENAMES.get(output_type, f"{output_type}.md")


def _print_terminal_summary(
    *,
    all_articles: list[Article],
    confirmed_articles: list[Article],
    selected_outputs: list[str],
    cost_estimate: CostEstimate,
    screenshot_results: list[dict],
    collector: ErrorCollector,
    uploaded_keys: list[str],
) -> None:
    """Print the end-of-run terminal summary."""
    failed_screenshots = [s for s in screenshot_results if not s.get("success")]

    print("\n" + "=" * 60)
    print("Magic Content Engine — Run Summary")
    print("=" * 60)
    print(f"  Articles found:    {len(all_articles)}")
    print(f"  Articles kept:     {len(confirmed_articles)}")
    print(f"  Outputs generated: {', '.join(selected_outputs) if selected_outputs else 'none'}")
    print(f"  Estimated cost:    ${cost_estimate.total_cost_usd:.6f}")
    print(f"  Files uploaded:    {len(uploaded_keys)}")

    if failed_screenshots:
        print(f"  Failed screenshots: {len(failed_screenshots)}")
        for s in failed_screenshots:
            print(f"    - {s.get('filename', '?')}: {s.get('error', 'unknown')}")

    if collector.has_errors:
        print(f"\n  Errors ({len(collector.errors)}):")
        for err in collector.errors:
            print(f"    [{err.step}] {err.target}: {err.error_message}")
    else:
        print("\n  Status: Clean run ✓")

    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Magic Content Engine — weekly content research and generation",
    )
    parser.add_argument(
        "--source",
        choices=["scheduled", "manual"],
        default="manual",
        help="Invocation source (default: manual)",
    )
    parser.add_argument(
        "--run-date",
        default=None,
        help="Override run date (YYYY-MM-DD). Defaults to today.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    run_date_val = date.today()
    if args.run_date:
        run_date_val = date.fromisoformat(args.run_date)

    logger.info("Magic Content Engine started (source=%s, date=%s)", args.source, run_date_val)

    # In production, real dependencies would be constructed here.
    # For now, log that workflow requires dependency injection.
    logger.info(
        "To run the full workflow, construct WorkflowDependencies and call run_workflow(). "
        "CLI stub exiting."
    )


if __name__ == "__main__":
    main()
