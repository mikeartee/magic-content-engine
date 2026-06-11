#!/usr/bin/env python3
"""Local runner for the Magic Content Engine bullpen pipeline.

Wires all six agent modules to real AWS-backed implementations (via the shared
``magic_content_engine.bullpen.wiring.build_agent_callables`` factory) and runs
the full pipeline locally. Requires AWS credentials configured for
ap-southeast-2 and the environment variables in .env to be set.

This terminal CLI supplies its own blocking ``input()``-based approval gate;
the shared factory is parameterised by that gate.

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
import logging
import os
import sys
from datetime import date
from pathlib import Path

# Ensure the repo root is on the path so magic_content_engine imports work
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env")

import boto3

from magic_content_engine import config
from magic_content_engine.bullpen.approval_gate import format_approval_email
from magic_content_engine.bullpen.editor_in_chief import run_pipeline
from magic_content_engine.bullpen.models import BullpenBrief, SubeditorReview
from magic_content_engine.bullpen.wiring import build_agent_callables

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
# Terminal approval gate — specific to the local CLI entry point.
# ---------------------------------------------------------------------------


class _BotoSESClient:
    """Wraps boto3 SES client for sending the approval-notification email."""

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

    if args.dry_run:
        logger.info("DRY RUN — using stub Researcher (no real crawl)")

    # Build the terminal-CLI approval gate and the shared agent callables.
    approval_fn = _make_approval_fn(output_dir, run_id)
    callables = build_agent_callables(
        brief=brief,
        output_dir=output_dir,
        approval_fn=approval_fn,
        github_token=github_token,
        dry_run=args.dry_run,
    )

    # Run the pipeline
    logger.info("Starting pipeline...")
    result = run_pipeline(brief=brief, **callables)

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
