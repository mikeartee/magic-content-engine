"""Unit tests for the cross-process control-file approval gate.

Covers Requirement 2 of the bullpen-console-go spec: the Bullpen ``approval_fn``
polls ``approval-decision.json`` in the shared run directory, returns ``True``
for an ``"approved"`` decision and ``False`` for ``"rejected"``, deletes the
file before returning, pre-cleans any stale decision before polling, retries on
partial/invalid JSON rather than crashing, and ignores unknown decision values.

The Console write is simulated either with a pre-seeded file or a background
thread that writes the decision after a short delay, exercising the poll loop.

Requirements: REQ-bullpen-console-go-2 (Cross-Process Approval Gate via Control File).
"""

from __future__ import annotations

import json
import threading
import time

import pytest

# Module under test.
from magic_content_engine.bullpen.control_file_gate import (
    make_control_file_approval_fn,
)

DECISION_FILENAME = "approval-decision.json"

# A small poll interval keeps the tests fast while still exercising the loop.
FAST_POLL = 0.02


def _write_decision(run_dir, decision, run_id="2025-01-15-kiro-ide"):
    path = run_dir / DECISION_FILENAME
    path.write_text(
        json.dumps(
            {
                "decision": decision,
                "decided_at": "2025-01-15T00:00:00+00:00",
                "run_id": run_id,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_approved_returns_true_and_deletes_file(tmp_path):
    """An 'approved' decision written after polling starts -> True, file consumed.

    The decision is written from a background thread *after* the gate begins
    polling, because the gate pre-cleans any file present before it starts (a
    pre-seeded file would be deleted by pre-clean and never honoured).
    """
    decision_path = tmp_path / DECISION_FILENAME
    gate = make_control_file_approval_fn(run_dir=tmp_path, poll_interval=FAST_POLL)

    def _console_writes():
        time.sleep(FAST_POLL * 3)
        _write_decision(tmp_path, "approved")

    writer = threading.Thread(target=_console_writes)
    writer.start()
    try:
        result = gate(None)
    finally:
        writer.join()

    assert result is True
    assert not decision_path.exists()


def test_rejected_returns_false_and_deletes_file(tmp_path):
    """A 'rejected' decision written after polling starts -> False, file consumed.

    Uses a background writer for the same reason as the approved case: the gate
    pre-cleans any pre-existing decision file before it begins polling.
    """
    decision_path = tmp_path / DECISION_FILENAME
    gate = make_control_file_approval_fn(run_dir=tmp_path, poll_interval=FAST_POLL)

    def _console_writes():
        time.sleep(FAST_POLL * 3)
        _write_decision(tmp_path, "rejected")

    writer = threading.Thread(target=_console_writes)
    writer.start()
    try:
        result = gate(None)
    finally:
        writer.join()

    assert result is False
    assert not decision_path.exists()


def test_preclean_removes_stale_file_before_polling(tmp_path):
    """A decision left over from an earlier gate must never be honoured.

    Seed a stale 'approved' file, then have a background thread write the real
    'rejected' decision shortly after. The gate must pre-clean the stale file,
    then return False from the freshly written decision.
    """
    _write_decision(tmp_path, "approved")  # stale leftover
    gate = make_control_file_approval_fn(run_dir=tmp_path, poll_interval=FAST_POLL)

    def _console_writes():
        time.sleep(FAST_POLL * 3)
        _write_decision(tmp_path, "rejected")

    writer = threading.Thread(target=_console_writes)
    writer.start()
    try:
        result = gate(None)
    finally:
        writer.join()

    assert result is False
    assert not (tmp_path / DECISION_FILENAME).exists()


def test_partial_json_is_retried_then_valid_decision_honoured(tmp_path):
    """Invalid/partial JSON must not crash the gate; it waits and retries.

    Seed a partial JSON file first, then a background thread overwrites it with
    a valid 'approved' decision. The gate must survive the partial read and
    eventually return True.
    """
    path = tmp_path / DECISION_FILENAME
    path.write_text('{"decision": "appro', encoding="utf-8")  # truncated JSON

    gate = make_control_file_approval_fn(run_dir=tmp_path, poll_interval=FAST_POLL)

    def _console_completes_write():
        time.sleep(FAST_POLL * 3)
        _write_decision(tmp_path, "approved")

    writer = threading.Thread(target=_console_completes_write)
    writer.start()
    try:
        result = gate(None)
    finally:
        writer.join()

    assert result is True
    assert not path.exists()


def test_unknown_decision_value_is_ignored_then_valid_honoured(tmp_path):
    """An unknown decision value is ignored; the gate keeps polling.

    Seed an unknown decision ('maybe'), then a background thread writes a valid
    'approved' decision. The gate must consume/ignore the unknown value and
    keep polling until it sees the valid one.
    """
    _write_decision(tmp_path, "maybe")
    gate = make_control_file_approval_fn(run_dir=tmp_path, poll_interval=FAST_POLL)

    def _console_writes_valid():
        time.sleep(FAST_POLL * 4)
        _write_decision(tmp_path, "approved")

    writer = threading.Thread(target=_console_writes_valid)
    writer.start()
    try:
        result = gate(None)
    finally:
        writer.join()

    assert result is True
    assert not (tmp_path / DECISION_FILENAME).exists()


def test_waits_for_file_to_appear(tmp_path):
    """With no file present, the gate blocks until the Console writes one."""
    gate = make_control_file_approval_fn(run_dir=tmp_path, poll_interval=FAST_POLL)

    def _console_writes_later():
        time.sleep(FAST_POLL * 5)
        _write_decision(tmp_path, "approved")

    writer = threading.Thread(target=_console_writes_later)
    writer.start()
    try:
        result = gate(None)
    finally:
        writer.join()

    assert result is True
    assert not (tmp_path / DECISION_FILENAME).exists()
