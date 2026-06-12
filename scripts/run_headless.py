#!/usr/bin/env python3
"""Headless runner for the Bullpen pipeline — the Console's spawn target.

The Go Bullpen Console spawns this script as a subprocess per Run. It wires the
pipeline via the shared ``build_agent_callables(...)`` factory with the
control-file approval gate (the Console writes ``approval-decision.json``; this
runner's gate polls for it). Progress is communicated solely through
``agent-log.jsonl`` in the run directory; stdout stays silent during normal
operation. On an unhandled exception the runner appends a synthetic
``pipeline_complete`` event (status=error, traceback) so the Console's tail
always sees a terminal event, and exits nonzero.

Args:
    --topic       Content topic (free text, required).
    --outputs     One or more of: blog youtube cfp usergroup digest all.
    --run-id      Run identifier; names the run directory.
    --output-dir  The per-run directory itself (output/<run-id>/), passed
                  whole by the Console's Run_Manager; agent-log.jsonl is
                  written directly inside it.

Requirements: REQ-bullpen-console-go-10 (Headless Runner Entry Point),
              REQ-bullpen-console-go-2 (Cross-Process Approval Gate).
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env")

import os

from magic_content_engine.bullpen.control_file_gate import make_control_file_approval_fn
from magic_content_engine.bullpen.editor_in_chief import run_pipeline
from magic_content_engine.bullpen.models import BullpenBrief
from magic_content_engine.bullpen.wiring import build_agent_callables

VALID_OUTPUTS = {"blog", "youtube", "cfp", "usergroup", "digest"}


def _parse_outputs(raw: list[str]) -> list[str]:
    if len(raw) == 1 and raw[0] == "all":
        return sorted(VALID_OUTPUTS)
    invalid = set(raw) - VALID_OUTPUTS
    if invalid:
        raise ValueError(f"Unknown output types: {invalid}. Valid: {VALID_OUTPUTS}")
    return raw


def _write_error_event(run_dir: Path, tb: str) -> None:
    """Append a synthetic pipeline_complete(status=error) so the Console tail
    always observes a terminal event even on an unhandled crash."""
    log_path = run_dir / "agent-log.jsonl"
    event = {
        "event_type": "pipeline_complete",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent_type": "editor_in_chief",
        "run_id": run_dir.name,
        "details": {
            "status": "error",
            "error": tb.splitlines()[-1] if tb else "Unknown error",
            "traceback": tb,
        },
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Headless Bullpen pipeline runner (spawned by the Console).",
    )
    parser.add_argument("--topic", required=True)
    parser.add_argument("--outputs", nargs="+", required=True, metavar="TYPE")
    parser.add_argument("--run-id", required=True, dest="run_id")
    parser.add_argument("--output-dir", required=True, dest="output_dir")
    parser.add_argument("--run-date", default=None, dest="run_date")
    args = parser.parse_args(argv)

    output_types = _parse_outputs(args.outputs)
    run_date_str = args.run_date or date.today().isoformat()
    run_date = date.fromisoformat(run_date_str)

    run_dir = Path(args.output_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    brief = BullpenBrief(
        topic=args.topic,
        requested_outputs=output_types,
        run_date=run_date,
    )

    approval_fn = make_control_file_approval_fn(run_dir=run_dir)
    callables = build_agent_callables(
        brief=brief,
        output_dir=str(run_dir),
        approval_fn=approval_fn,
        github_token=os.getenv("GITHUB_TOKEN"),
    )

    try:
        result = run_pipeline(brief=brief, **callables)
    except Exception:
        tb = traceback.format_exc()
        # Allowed to surface diagnostics to stderr during abnormal operation.
        print(tb, file=sys.stderr)
        _write_error_event(run_dir, tb)
        return 1

    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
