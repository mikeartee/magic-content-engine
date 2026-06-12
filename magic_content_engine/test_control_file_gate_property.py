"""Property test for the cross-process approval round-trip (Requirement 2.1).

Property 2 of the bullpen-console-go design ("Approval decision round-trips and
self-cleans"):

    for all d in {approved, rejected}, an atomic Console-style write of d
    followed by the Bullpen poller's read-and-delete returns (d == approved) as
    the boolean and leaves no approval-decision.json (and no .tmp sibling) in the
    run directory.

The decision is written with the SAME atomic mechanism the Go Console uses (a
``.tmp`` sibling renamed into place via ``os.replace``), and is written from a
background thread that fires AFTER the gate begins polling — the gate pre-cleans
any pre-existing file, so a pre-seeded decision would never be honoured, and a
background writer also avoids the historical hang (the gate always sees a
decision appear). Run with ``-o faulthandler_timeout=30 -p no:cacheprovider``.

Requirements: REQ-bullpen-console-go-2 (Cross-Process Approval Gate via Control
File).
"""

from __future__ import annotations

import json
import os
import threading
import time

from hypothesis import given, settings
from hypothesis import strategies as st

from magic_content_engine.bullpen.control_file_gate import (
    make_control_file_approval_fn,
)

DECISION_FILENAME = "approval-decision.json"
TMP_FILENAME = DECISION_FILENAME + ".tmp"

# A small poll interval keeps each example fast while still exercising the loop.
FAST_POLL = 0.02


def _atomic_write_decision(run_dir, approved: bool, run_id: str = "prop-run") -> None:
    """Write the decision the way the Go Console does: temp file + os.replace."""
    decision = "approved" if approved else "rejected"
    payload = json.dumps(
        {
            "decision": decision,
            "decided_at": "2025-01-15T00:00:00+00:00",
            "run_id": run_id,
        }
    )
    tmp = run_dir / TMP_FILENAME
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, run_dir / DECISION_FILENAME)


@settings(max_examples=25, deadline=None)
@given(approved=st.booleans())
def test_write_then_poll_round_trips_and_leaves_no_file(tmp_path_factory, approved):
    run_dir = tmp_path_factory.mktemp("gate")
    gate = make_control_file_approval_fn(run_dir=run_dir, poll_interval=FAST_POLL)

    def _console_writes():
        time.sleep(FAST_POLL * 2)
        _atomic_write_decision(run_dir, approved)

    writer = threading.Thread(target=_console_writes)
    writer.start()
    try:
        result = gate(None)
    finally:
        writer.join()

    # The poller returns the matching boolean ...
    assert result is approved
    # ... and leaves no decision file (consumed) nor any temp sibling behind.
    assert not (run_dir / DECISION_FILENAME).exists()
    assert not (run_dir / TMP_FILENAME).exists()
