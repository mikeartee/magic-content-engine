"""
Agent callable factory for the GUI pipeline runner.

Wires up the same agent callables as ``scripts/run_local.py`` but uses
the run_state's output_dir rather than a CLI argument.

This module is imported lazily inside the pipeline thread to keep AWS
client construction off the critical path at server startup.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


# ---------------------------------------------------------------------------
# Bedrock LLM helper
# ---------------------------------------------------------------------------


def _make_bedrock_llm(region: str = "ap-southeast-2"):
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
# DynamoDB log and checkpoint helpers
# ---------------------------------------------------------------------------


def _make_log_fn(output_dir: str):
    """Return a log_fn that writes AMILogEvents to the local JSONL file
    and best-effort to DynamoDB."""
    import dataclasses

    ddb = boto3.client("dynamodb", region_name="ap-southeast-2")
    log_path = Path(output_dir) / "agent-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log_fn(event: AMILogEvent) -> None:
        line = json.dumps(dataclasses.asdict(event), default=str)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

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


def _make_checkpoint_fn(output_dir: str):
    """Return a checkpoint_fn that writes Checkpoints locally and to DynamoDB."""
    import dataclasses

    ddb = boto3.client("dynamodb", region_name="ap-southeast-2")
    cp_path = Path(output_dir) / "checkpoints.json"
    cp_path.parent.mkdir(parents=True, exist_ok=True)

    def _checkpoint_fn(checkpoint: Checkpoint) -> None:
        existing: list[dict] = []
        if cp_path.exists():
            try:
                existing = json.loads(cp_path.read_text())
            except Exception:
                pass
        existing.append(dataclasses.asdict(checkpoint))
        cp_path.write_text(json.dumps(existing, indent=2, default=str))

        try:
            run_id = cp_path.parent.name
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
# Agent callable factories
# ---------------------------------------------------------------------------


def _make_researcher_fn(github_token: str | None):
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
            "Researcher complete — %d articles from %d sources (%d failed)",
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


def _make_desk_editor_fn():
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
        logger.info("Desk Editor complete — angle: %s", brief.editorial_angle[:60])
        return brief

    return _desk_editor_fn


def _make_writer_fn(output_dir: str):
    llm = _make_bedrock_llm()

    def _writer_fn(
        content_brief: ContentBrief,
        revision_feedback: str | None,
    ) -> WriterManifest:
        logger.info("Writer starting — %d output types", len(content_brief.output_types))
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
    llm = _make_bedrock_llm()

    def _subeditor_fn(manifest: WriterManifest):
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
    class _BotoS3Client:
        def __init__(self) -> None:
            self._client = boto3.client("s3", region_name="ap-southeast-2")

        def upload_file(self, local_path: str, bucket: str, key: str) -> None:
            self._client.upload_file(local_path, bucket, key)

    class _BotoSESClient:
        def __init__(self) -> None:
            self._client = boto3.client("ses", region_name="ap-southeast-2")

        def send_email(self, sender: str, recipient: str, subject: str, body: str) -> None:
            self._client.send_email(
                Source=sender,
                Destination={"ToAddresses": [recipient]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                },
            )

    s3_client = _BotoS3Client()
    ses_client = _BotoSESClient()
    s3_key_prefix = f"output/{run_date}-{slug}/"

    def _publisher_fn(approved_files: list[str]) -> None:
        logger.info("Publisher starting — %d files", len(approved_files))
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
# Public factory
# ---------------------------------------------------------------------------


def build_agent_callables(run_state: object, brief: BullpenBrief) -> dict:
    """Build all agent callables for a pipeline run.

    Returns a dict suitable for ``**kwargs`` unpacking into ``run_pipeline()``.
    """
    output_dir: str = getattr(run_state, "output_dir", "output")
    github_token: str | None = os.getenv("GITHUB_TOKEN")

    # Derive run_date and slug from the brief
    run_date = brief.run_date.isoformat() if hasattr(brief, "run_date") else ""
    slug = brief.topic[:40].lower().replace(" ", "-").replace("/", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-").strip("-")

    return {
        "researcher_fn": _make_researcher_fn(github_token),
        "desk_editor_fn": _make_desk_editor_fn(),
        "writer_fn": _make_writer_fn(output_dir),
        "subeditor_fn": _make_subeditor_fn(output_dir),
        "publisher_fn": _make_publisher_fn(output_dir, run_date, slug),
        "log_fn": _make_log_fn(output_dir),
        "checkpoint_fn": _make_checkpoint_fn(output_dir),
    }
