#!/usr/bin/env python3
"""Local runner for the Magic Content Engine bullpen pipeline.

Wires all six agent modules to real AWS-backed implementations and runs
the full pipeline locally. Requires AWS credentials configured for
ap-southeast-2 and the environment variables in .env to be set.

Usage:
    python scripts/run_local.py --topic "Kiro IDE 1.0 launch" --outputs blog youtube
    python scripts/run_local.py --topic "AgentCore GA" --outputs all
    python scripts/run_local.py --topic "Strands SDK update" --outputs digest

Output types: blog, youtube, cfp, usergroup, digest, all

Prerequisites:
    1. AWS credentials configured (aws configure or AWS_PROFILE set)
    2. .env file populated — copy .env.example and fill in real values:
       - APPROVAL_TOKEN_SECRET  (generate: python -c "import secrets; print(secrets.token_hex(32))")
       - SES_SENDER_EMAIL       (must be verified in SES ap-southeast-2)
       - SES_RECIPIENT_EMAIL    (where approval emails go — your email)
       - GITHUB_TOKEN           (GitHub PAT with repo read scope)
       - VAULT_PATH             (absolute path to your Obsidian vault)
    3. Infrastructure provisioned:
       python scripts/create_infrastructure.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# Ensure the repo root is on the path so magic_content_engine imports work
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env")

import boto3

from magic_content_engine import config
from magic_content_engine.bullpen.approval_gate import format_approval_email, send_approval_email
from magic_content_engine.bullpen.archivist import run as run_archivist
from magic_content_engine.bullpen.desk_editor import run_desk_editor
from magic_content_engine.bullpen.editor_in_chief import run_pipeline
from magic_content_engine.bullpen.models import (
    AMILogEvent,
    BullpenBrief,
    Checkpoint,
    ContentBrief,
    ResearchBrief,
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

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_local")

# ---------------------------------------------------------------------------
# Output type mapping
# ---------------------------------------------------------------------------

VALID_OUTPUTS = {"blog", "youtube", "cfp", "usergroup", "digest"}


def _parse_outputs(raw: list[str]) -> list[str]:
    if len(raw) == 1 and raw[0] == "all":
        return sorted(VALID_OUTPUTS)
    invalid = set(raw) - VALID_OUTPUTS
    if invalid:
        raise ValueError(f"Unknown output types: {invalid}. Valid: {VALID_OUTPUTS}")
    return raw


# ---------------------------------------------------------------------------
# Bedrock LLM helper — shared across agents
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


def _make_dynamodb_log_fn(run_dir: str):
    """Return a log_fn that writes AMILogEvents to DynamoDB mce-run-history
    AND appends them as JSON Lines to agent-log.jsonl in the run directory."""
    import dataclasses

    ddb = boto3.client("dynamodb", region_name="ap-southeast-2")
    log_path = Path(run_dir) / "agent-log.jsonl"
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


def _make_dynamodb_checkpoint_fn(run_dir: str):
    """Return a checkpoint_fn that writes Checkpoints to DynamoDB mce-checkpoints
    AND to checkpoints.json in the run directory."""
    import dataclasses

    ddb = boto3.client("dynamodb", region_name="ap-southeast-2")
    cp_path = Path(run_dir) / "checkpoints.json"
    cp_path.parent.mkdir(parents=True, exist_ok=True)

    def _checkpoint_fn(checkpoint: Checkpoint) -> None:
        # Write to local JSON file
        existing = []
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
# S3 and SES client wrappers
# ---------------------------------------------------------------------------


class _BotoS3Client:
    """Wraps boto3 S3 client to match S3ClientProtocol."""

    def __init__(self, region: str = "ap-southeast-2") -> None:
        self._client = boto3.client("s3", region_name=region)

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        self._client.upload_file(local_path, bucket, key)


class _BotoSESClient:
    """Wraps boto3 SES client to match SESClientProtocol."""

    def __init__(self, region: str = "ap-southeast-2") -> None:
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
# Agent callable factories
# ---------------------------------------------------------------------------


def _make_researcher_fn(github_token: str | None, output_dir: str):
    """Return a researcher_fn(brief) -> ResearchBrief."""
    llm = _make_bedrock_scorer()
    collector = ErrorCollector()

    def _researcher_fn(brief: BullpenBrief) -> ResearchBrief:
        from magic_content_engine.bullpen.researcher import ResearchBrief as RB
        from datetime import datetime, timezone

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
        return RB(
            articles=scored,
            sources_crawled=sources_crawled,
            sources_failed=sources_failed,
            run_timestamp=datetime.now(timezone.utc).isoformat(),
        )

    return _researcher_fn


def _make_desk_editor_fn(output_types: list[str]):
    """Return a desk_editor_fn(research_brief, topic, output_types) -> ContentBrief."""

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


def _make_approval_fn(output_dir: str, run_id: str):
    """Return an approval_fn(review) -> bool.

    Sends an SES email with approve/reject URLs and waits for terminal input
    as a local fallback (since we don't have API Gateway wired up yet for
    local runs). In production this would be replaced by the durable wait().
    """
    ses_client = _BotoSESClient()

    def _approval_fn(sub_review: SubeditorReview | None) -> bool:
        if sub_review is None:
            logger.warning("Approval gate called with no review — auto-approving")
            return True

        # Try to send the approval email
        try:
            content = format_approval_email(
                verdicts=sub_review.verdicts,
                output_dir=output_dir,
                run_id=run_id,
            )
            ses_client.send_email(
                sender=config.SES_SENDER_EMAIL,
                recipient=config.SES_RECIPIENT_EMAIL,
                subject=content.subject,
                body=content.body_text,
            )
            logger.info("Approval email sent to %s", config.SES_RECIPIENT_EMAIL)
        except Exception as exc:
            logger.warning("Could not send approval email: %s", exc)

        # Print summary to terminal
        print("\n" + "=" * 60)
        print("APPROVAL GATE")
        print("=" * 60)
        if sub_review.verdicts:
            for v in sub_review.verdicts:
                icon = "✓" if v.verdict == "publish" else ("✗" if v.verdict == "spike" else "⚠")
                print(f"  {icon} {v.filename} → {v.verdict}")
                if v.feedback:
                    print(f"    {v.feedback[:100]}")
        else:
            print("  No files to review.")
        print("=" * 60)

        response = input("\nApprove publication? [y/N]: ").strip().lower()
        return response in ("y", "yes")

    return _approval_fn


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run the Magic Content Engine bullpen pipeline locally.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--topic",
        required=True,
        help='Content topic, e.g. "Kiro IDE 1.0 launch"',
    )
    parser.add_argument(
        "--outputs",
        nargs="+",
        default=["blog", "youtube"],
        metavar="TYPE",
        help="Output types: blog youtube cfp usergroup digest all (default: blog youtube)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(_REPO_ROOT / "output"),
        help="Local directory for generated content (default: ./output)",
    )
    parser.add_argument(
        "--run-date",
        default=None,
        help="Override run date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Researcher crawl and use a minimal stub brief instead",
    )
    args = parser.parse_args(argv)

    # Validate outputs
    try:
        output_types = _parse_outputs(args.outputs)
    except ValueError as exc:
        parser.error(str(exc))

    run_date_str = args.run_date or date.today().isoformat()
    run_date = date.fromisoformat(run_date_str)

    # Derive slug from topic
    slug = args.topic[:40].lower().replace(" ", "-").replace("/", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-").strip("-")

    run_id = f"{run_date_str}-{slug}"
    output_dir = args.output_dir

    logger.info("=" * 60)
    logger.info("Magic Content Engine — Local Run")
    logger.info("  Topic      : %s", args.topic)
    logger.info("  Outputs    : %s", output_types)
    logger.info("  Run date   : %s", run_date_str)
    logger.info("  Run ID     : %s", run_id)
    logger.info("  Output dir : %s", output_dir)
    logger.info("=" * 60)

    # Validate required env vars
    missing = []
    for var in ("APPROVAL_TOKEN_SECRET", "SES_SENDER_EMAIL", "SES_RECIPIENT_EMAIL"):
        if not os.getenv(var):
            missing.append(var)
    if missing:
        logger.error(
            "Missing required environment variables: %s\n"
            "Copy .env.example to .env and fill in the values.",
            ", ".join(missing),
        )
        sys.exit(1)

    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        logger.warning(
            "GITHUB_TOKEN not set — GitHub API sources will use unauthenticated "
            "requests (rate-limited to 60/hour)"
        )

    # Build the BullpenBrief
    brief = BullpenBrief(
        topic=args.topic,
        requested_outputs=output_types,
        run_date=run_date,
    )

    # Build log and checkpoint functions
    log_fn = _make_dynamodb_log_fn(output_dir)
    checkpoint_fn = _make_dynamodb_checkpoint_fn(output_dir)

    # Build agent callables
    if args.dry_run:
        logger.info("DRY RUN — using stub Researcher (no real crawl)")
        from magic_content_engine.bullpen.models import ScoredArticle, ResearchBrief as RB

        def _stub_researcher(brief: BullpenBrief) -> RB:
            return RB(
                articles=[
                    ScoredArticle(
                        title="Kiro IDE changelog update",
                        url="https://kiro.dev/changelog/ide/",
                        source="kiro.dev/changelog/ide/",
                        relevance_score=5,
                        summary="Kiro IDE released new features including improved spec workflow, hooks, and steering file support for AI-assisted development.",
                    )
                ],
                sources_crawled=["kiro.dev/changelog/ide/"],
                sources_failed=[],
                run_timestamp=datetime.now(timezone.utc).isoformat(),
            )

        researcher_fn = _stub_researcher
    else:
        researcher_fn = _make_researcher_fn(github_token, output_dir)

    desk_editor_fn = _make_desk_editor_fn(output_types)
    writer_fn = _make_writer_fn(output_dir)
    subeditor_fn = _make_subeditor_fn(output_dir)
    publisher_fn = _make_publisher_fn(output_dir, run_date_str, slug)
    approval_fn = _make_approval_fn(output_dir, run_id)

    # Run the pipeline
    logger.info("Starting pipeline...")
    result = run_pipeline(
        brief=brief,
        researcher_fn=researcher_fn,
        desk_editor_fn=desk_editor_fn,
        writer_fn=writer_fn,
        subeditor_fn=subeditor_fn,
        publisher_fn=publisher_fn,
        approval_fn=approval_fn,
        log_fn=log_fn,
        checkpoint_fn=checkpoint_fn,
    )

    # Print final summary
    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)
    print(f"  Status     : {result.status}")
    print(f"  Published  : {len(result.files_published)} file(s)")
    print(f"  Escalated  : {len(result.files_escalated)} file(s)")
    print(f"  Errors     : {len(result.errors)}")
    if result.files_published:
        print("\n  Published files:")
        for f in result.files_published:
            print(f"    {f}")
    if result.files_escalated:
        print("\n  Escalated for manual review:")
        for f in result.files_escalated:
            print(f"    {f}")
    if result.errors:
        print("\n  Errors:")
        for e in result.errors:
            print(f"    [{e.get('step', '?')}] {e.get('error', e)}")
    print(f"\n  Output dir : {output_dir}")
    print(f"  Log file   : {output_dir}/agent-log.jsonl")
    print("=" * 60)

    sys.exit(0 if result.status == "success" else 1)


if __name__ == "__main__":
    main()
