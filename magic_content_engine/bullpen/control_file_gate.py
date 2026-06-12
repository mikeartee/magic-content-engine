"""Cross-process control-file approval gate for the headless runner.

The Go Console and the Python Bullpen pipeline run in separate processes and
communicate only through files in the shared run directory
(``output/<run_id>/``). This module implements the Bullpen side of the approval
handshake: an ``approval_fn`` that polls for ``approval-decision.json``, reads
the decision, and deletes the file before returning.

Protocol (see design.md "Control-File approval_fn"):
  - Pre-clean: delete any pre-existing decision file before polling, so a
    decision from an earlier gate is never honoured.
  - Poll at ~1 second intervals for ``approval-decision.json`` to appear.
  - On partial or invalid JSON, wait one interval and retry (the Console writes
    atomically via a ``.tmp`` rename, but the poller must never crash on a
    racing read).
  - ``decision == "approved"`` -> return ``True``; ``"rejected"`` -> ``False``.
    Delete the file before returning either way.
  - Any unknown decision value is ignored; the gate consumes the file and keeps
    polling.

The decision file schema is::

    {"decision": "approved" | "rejected",
     "decided_at": <ISO 8601 timestamp>,
     "run_id": <active run id>}

Requirements: REQ-bullpen-console-go-2 (Cross-Process Approval Gate via Control
File), REQ-bullpen-console-go-10 (Headless Runner Entry Point).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DECISION_FILENAME = "approval-decision.json"
DEFAULT_POLL_INTERVAL = 1.0  # seconds; ~1s latency acceptable per ADR-0001


def make_control_file_approval_fn(
    *,
    run_dir: str | Path,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> Callable[[object], bool]:
    """Build a control-file ``approval_fn`` bound to *run_dir*.

    Parameters
    ----------
    run_dir:
        The shared run directory (``output/<run_id>/``) that both the Console
        and the Bullpen pipeline read from and write to.
    poll_interval:
        Seconds to sleep between polls. Defaults to ~1s; tests may shorten it.

    Returns
    -------
    Callable[[SubeditorReview | None], bool]
        A blocking gate that returns ``True`` for an approved decision and
        ``False`` for a rejected one. The ``sub_review`` argument is accepted to
        match the pipeline's ``approval_fn`` signature but is unused — the
        Console has already rendered the verdicts from ``agent-log.jsonl``.
    """
    decision_path = Path(run_dir) / DECISION_FILENAME

    def _approval_fn(sub_review: object = None) -> bool:
        # Pre-clean: a leftover file from an earlier gate must not be honoured.
        if decision_path.exists():
            logger.debug("Pre-cleaning stale decision file: %s", decision_path)
            _safe_unlink(decision_path)

        while True:
            if decision_path.exists():
                try:
                    raw = decision_path.read_text(encoding="utf-8")
                    data = json.loads(raw)
                except (OSError, ValueError):
                    # Partial/invalid read (writer not finished, or transient
                    # filesystem race) — wait and retry rather than crash.
                    time.sleep(poll_interval)
                    continue

                decision = data.get("decision") if isinstance(data, dict) else None

                if decision == "approved":
                    _safe_unlink(decision_path)  # consume the decision
                    return True
                if decision == "rejected":
                    _safe_unlink(decision_path)  # consume the decision
                    return False

                # Unknown value — consume and keep polling so a malformed
                # decision can never stall or wrongly resolve the gate.
                logger.warning(
                    "Ignoring unknown approval decision %r in %s",
                    decision,
                    decision_path,
                )
                _safe_unlink(decision_path)
                time.sleep(poll_interval)
                continue

            time.sleep(poll_interval)

    return _approval_fn


def _safe_unlink(path: Path) -> None:
    """Delete *path*, tolerating a concurrent delete."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
