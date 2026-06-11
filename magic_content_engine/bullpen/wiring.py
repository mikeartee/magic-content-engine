"""Shared agent wiring factory for the Bullpen pipeline.

This module is the single place all agent callables are wired for
``editor_in_chief.run_pipeline()``. It collapses the agent wiring that was
previously triplicated across ``scripts/run_local.py`` and
``scripts/gui/_agent_factory.py`` into one ``build_agent_callables(...)``
factory, parameterised by the ``approval_fn`` each entry point supplies.

The factory preserves the existing AWS-backed callables exactly as they
behaved before the extraction:
  - Bedrock LLM inference (researcher scorer, writer, subeditor)
  - DynamoDB ``mce-run-history`` log writes + local ``agent-log.jsonl``
  - DynamoDB ``mce-checkpoints`` writes + local ``checkpoints.json``
  - S3 upload + SES email via the Publisher

AWS lives on the Python side; nothing here changes the inference backend or
the DynamoDB run-history/checkpoint writes used by Runs.

Requirements: REQ-bullpen-console-go-9 (Shared Wiring Factory Refactor).
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import boto3

from magic_content_engine import config
from magic_content_engine.bullpen.desk_editor import run_desk_editor
from magic_content_engine.bullpen.models import (
    AMILogEvent,
    BullpenBrief,
    Checkpoint,
    ContentBrief,
    ResearchBrief,
    ScoredArticle,
    SubeditorReview,
    WriterInput,
    WriterManifest,
)
from magic_content_engine.bullpen.publisher import publish
from magic_content_engine.bullpen.researcher import (
    _make_bedrock_scorer,
    crawl_all_sources,
    score_articles,
)
from magic_content_engine.bullpen.subeditor import review as subeditor_review
from magic_content_engine.bullpen.writer import run_writer
from magic_content_engine.errors import ErrorCollector

logger = logging.getLogger(__name__)

_DEFAULT_REGION = "ap-southeast-2"


# ---------------------------------------------------------------------------
# Slug derivation — shared so every entry point derives the run slug
# identically from the brief topic.
# ---------------------------------------------------------------------------


def _derive_slug(topic: str) -> str:
    slug = topic[:40].lower().replace(" ", "-").replace("/", "-")
    return "".join(c for c in slug if c.isalnum() or c == "-").strip("-")


# ---------------------------------------------------------------------------
# Bedrock LLM helper — shared across the writer and subeditor agents.
# ---------------------------------------------------------------------------


def _make_bedrock_llm(region: str = _DEFAULT_REGION):
    """Return a Bedrock LLM callable matching the LLMProtocol interface."""
    client = boto3.client("bedrock-runtime", region_name=region)

    def _call(*, model_id: str, prompt: str) -> str:
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        })
        response = client.invoke_model(
            modelId=model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(response["body"].read())
        return result["content"][0]["text"]

    return _call


# ---------------------------------------------------------------------------
# S3 and SES client wrappers (match S3ClientProtocol / SESClientProtocol).
# ---------------------------------------------------------------------------


class _BotoS3Client:
    """Wraps boto3 S3 client to match S3ClientProtocol."""

    def __init__(self, region: str = _DEFAULT_REGION) -> None:
        self._client = boto3.client("s3", region_name=region)

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        self._client.upload_file(local_path, bucket, key)


class _BotoSESClient:
    """Wraps boto3 SES client to match SESClientProtocol."""

    def __init__(self, region: str = _DEFAULT_REGION) -> None:
        self._client = boto3.client("ses", region_name=region)

    def send_email(self, sender: str, recipient: str, subject: str, body: str) -> None:
        self._client.send_email(
            Source=sender,
            Destination={"ToAddresses": [recipient]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        )


# ---------------------------------------------------------------------------
# DynamoDB log and checkpoint helpers.
# ---------------------------------------------------------------------------


def _make_dynamodb_log_fn(output_dir: str):
    """Return a log_fn that writes AMILogEvents to DynamoDB mce-run-history
    AND appends them as JSON Lines to agent-log.jsonl in the run directory."""
    ddb = boto3.client("dynamodb", region_name=_DEFAULT_REGION)
    log_path = Path(output_dir) / "agent-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log_fn(event: AMILogEvent) -> None:
        # Write to local JSONL file
        line = json.dumps(dataclasses.asdict(event), default=str)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

        # Write to DynamoDB (best-effort — don't crash pipeline on log failure)
        try:
            ddb.put_item(
                TableName=config.MCE_RUN_HISTORY_TABLE,
                Item={
                    "run_id": {"S": event.run_id or "unknown"},
                    "timestamp": {"S": event.timestamp},
                    "event_type": {"S": event.event_type},
                    "agent_type": {"S": event.agent_type},
                    "details": {"S": json.dumps(event.details, default=str)},
                },
            )
        except Exception as exc:
            logger.warning("DynamoDB log write failed (non-fatal): %s", exc)

    return _log_fn


def _make_dynamodb_checkpoint_fn(output_dir: str):
    """Return a checkpoint_fn that writes Checkpoints to DynamoDB mce-checkpoints
    AND to checkpoints.json in the run directory."""
    ddb = boto3.client("dynamodb", region_name=_DEFAULT_REGION)
    cp_path = Path(output_dir) / "checkpoints.json"
    cp_path.parent.mkdir(parents=True, exist_ok=True)

    def _checkpoint_fn(checkpoint: Checkpoint) -> None:
        # Write to local JSON file
        existing: list[dict] = []
        if cp_path.exists():
            try:
                existing = json.loads(cp_path.read_text())
            except Exception:
                pass
        existing.append(dataclasses.asdict(checkpoint))
        cp_path.write_text(json.dumps(existing, indent=2, default=str))

        # Write to DynamoDB (best-effort)
        try:
            run_id = cp_path.parent.name  # use run dir name as run_id
            ddb.put_item(
                TableName=config.MCE_CHECKPOINTS_TABLE,
                Item={
                    "run_id": {"S": run_id},
                    "agent_type": {"S": checkpoint.agent_type},
                    "completion_timestamp": {"S": checkpoint.completion_timestamp},
                    "output_hash": {"S": checkpoint.output_hash},
                    "status": {"S": checkpoint.status},
                },
            )
        except Exception as exc:
            logger.warning("DynamoDB checkpoint write failed (non-fatal): %s", exc)

    return _checkpoint_fn


# ---------------------------------------------------------------------------
# Agent callable factories.
# ---------------------------------------------------------------------------


def _make_researcher_fn(github_token: str | None):
    """Return a researcher_fn(brief) -> ResearchBrief."""
    llm = _make_bedrock_scorer()
    collector = ErrorCollector()

    def _researcher_fn(brief: BullpenBrief) -> ResearchBrief:
        logger.info("Researcher starting — topic: %s", brief.topic)
        raw_articles, sources_crawled, sources_failed = crawl_all_sources(
            github_token=github_token,
            collector=collector,
        )
        scored = score_articles(raw_articles, llm=llm, collector=collector)
        logger.info(
            "Researcher complete — %d articles kept from %d sources (%d failed)",
            len(scored),
            len(sources_crawled),
            len(sources_failed),
        )
        return ResearchBrief(
            articles=scored,
            sources_crawled=sources_crawled,
            sources_failed=sources_failed,
            run_timestamp=datetime.now(timezone.utc).isoformat(),
        )

    return _researcher_fn


def _make_stub_researcher_fn():
    """Return a researcher_fn that skips the real crawl (dry-run mode)."""

    def _stub_researcher(brief: BullpenBrief) -> ResearchBrief:
        return ResearchBrief(
            articles=[
                ScoredArticle(
                    title="Kiro IDE changelog update",
                    url="https://kiro.dev/changelog/ide/",
                    source="kiro.dev/changelog/ide/",
                    relevance_score=5,
                    summary=(
                        "Kiro IDE released new features including improved spec "
                        "workflow, hooks, and steering file support for "
                        "AI-assisted development."
                    ),
                )
            ],
            sources_crawled=["kiro.dev/changelog/ide/"],
            sources_failed=[],
            run_timestamp=datetime.now(timezone.utc).isoformat(),
        )

    return _stub_researcher


def _make_desk_editor_fn():
    """Return a desk_editor_fn(research_brief, topic, requested_outputs) -> ContentBrief."""

    def _desk_editor_fn(
        research_brief: ResearchBrief,
        topic: str,
        requested_outputs: list[str],
    ) -> ContentBrief:
        logger.info("Desk Editor starting — %d articles", len(research_brief.articles))
        brief = run_desk_editor(
            research_brief=research_brief,
            topic=topic,
            output_types=requested_outputs,
            steering_base_path=config.STEERING_BASE_PATH,
        )
        logger.info(
            "Desk Editor complete — %d articles selected, angle: %s",
            len(brief.selected_articles),
            brief.editorial_angle[:60],
        )
        return brief

    return _desk_editor_fn


def _make_writer_fn(output_dir: str):
    """Return a writer_fn(content_brief, revision_feedback) -> WriterManifest."""
    llm = _make_bedrock_llm()

    def _writer_fn(
        content_brief: ContentBrief,
        revision_feedback: str | None,
    ) -> WriterManifest:
        logger.info(
            "Writer starting — %d output types", len(content_brief.output_types)
        )
        writer_input = WriterInput(
            content_brief=content_brief,
            steering_base_path=config.STEERING_BASE_PATH,
            output_dir=output_dir,
            revision_feedback=revision_feedback,
        )
        manifest = run_writer(writer_input, llm)
        logger.info("Writer complete — %d files written", len(manifest.files_written))
        return manifest

    return _writer_fn


def _make_subeditor_fn(output_dir: str):
    """Return a subeditor_fn(manifest) -> SubeditorReview."""
    llm = _make_bedrock_llm()

    def _subeditor_fn(manifest: WriterManifest) -> SubeditorReview:
        logger.info("Subeditor starting — %d files", len(manifest.files_written))
        result = subeditor_review(
            manifest=manifest,
            output_dir=output_dir,
            llm=llm,
            steering_base_path=config.STEERING_BASE_PATH,
        )
        for v in result.verdicts:
            logger.info("  %s → %s", v.filename, v.verdict)
        return result

    return _subeditor_fn


def _make_publisher_fn(output_dir: str, run_date: str, slug: str):
    """Return a publisher_fn(approved_files) -> None."""
    s3_client = _BotoS3Client()
    ses_client = _BotoSESClient()
    s3_key_prefix = f"output/{run_date}-{slug}/"

    def _publisher_fn(approved_files: list[str]) -> None:
        logger.info("Publisher starting — %d files", len(approved_files))
        # Resolve relative paths to absolute paths under output_dir
        abs_files = []
        for f in approved_files:
            p = Path(f)
            if not p.is_absolute():
                p = Path(output_dir) / f
            abs_files.append(str(p))

        report = publish(
            approved_files=abs_files,
            s3_key_prefix=s3_key_prefix,
            bucket=config.MCE_SECOND_BRAIN_BUCKET,
            s3_client=s3_client,
            ses_client=ses_client,
            sender_email=config.SES_SENDER_EMAIL,
            recipient_email=config.SES_RECIPIENT_EMAIL,
        )
        logger.info(
            "Publisher complete — %d files uploaded, email_sent=%s",
            len(report.files_uploaded),
            report.email_sent,
        )

    return _publisher_fn


# ---------------------------------------------------------------------------
# Public factory.
# ---------------------------------------------------------------------------


def build_agent_callables(
    *,
    brief: BullpenBrief,
    output_dir: str,
    approval_fn: Callable[[SubeditorReview | None], bool],
    github_token: str | None = None,
    dry_run: bool = False,
) -> dict[str, Callable]:
    """Build every agent callable for ``run_pipeline()``, in one place.

    Returns a dict ready for ``**kwargs`` unpacking into ``run_pipeline()``:
        researcher_fn, desk_editor_fn, writer_fn, subeditor_fn,
        publisher_fn, approval_fn, log_fn, checkpoint_fn

    Parameterised by ``approval_fn`` so each entry point supplies its own gate:
      - terminal CLI    -> blocking input() gate
      - headless runner -> control-file gate (polls approval-decision.json)
      - legacy GUI      -> threading.Event gate

    The AWS-backed callables (Bedrock LLM, DynamoDB log/checkpoint, S3/SES
    publisher) are preserved exactly as they behaved before this extraction.
    When ``dry_run`` is true the Researcher is replaced by a stub that skips
    the real crawl; every other callable is wired identically.
    """
    run_date = brief.run_date.isoformat() if hasattr(brief, "run_date") else ""
    slug = _derive_slug(brief.topic)

    if dry_run:
        logger.info("DRY RUN — using stub Researcher (no real crawl)")
        researcher_fn: Callable = _make_stub_researcher_fn()
    else:
        researcher_fn = _make_researcher_fn(github_token)

    return {
        "researcher_fn": researcher_fn,
        "desk_editor_fn": _make_desk_editor_fn(),
        "writer_fn": _make_writer_fn(output_dir),
        "subeditor_fn": _make_subeditor_fn(output_dir),
        "publisher_fn": _make_publisher_fn(output_dir, run_date, slug),
        "approval_fn": approval_fn,
        "log_fn": _make_dynamodb_log_fn(output_dir),
        "checkpoint_fn": _make_dynamodb_checkpoint_fn(output_dir),
    }
