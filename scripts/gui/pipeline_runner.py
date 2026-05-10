"""
Bullpen Web GUI — pipeline background thread and approval gate.

This module is responsible for:
- Running ``run_pipeline()`` from ``magic_content_engine.bullpen.editor_in_chief``
  in a background thread so the Flask server stays responsive.
- Providing ``_make_approval_fn``, which replaces the terminal ``input()`` gate
  in ``run_local.py`` with a ``threading.Event`` that the GUI signals via
  ``POST /api/run/approve`` or ``POST /api/run/reject``.

Full implementation in Task 3. This stub exposes ``run_pipeline_thread`` so
that ``app.py`` can reference it before Task 3 is merged.
"""

from __future__ import annotations

# Standard library
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from magic_content_engine.bullpen.models import BullpenBrief


def run_pipeline_thread(run_state: object, brief: "BullpenBrief") -> None:
    """Stub — full implementation in Task 3.

    Sets ``run_state.in_progress = True`` (already set by the caller) and
    immediately clears it so the state machine stays consistent during tests.
    The real implementation will invoke ``run_pipeline()`` here.
    """
    # Task 3 will replace this body with the real pipeline invocation.
    try:
        pass  # placeholder for run_pipeline() call
    finally:
        run_state.in_progress = False  # type: ignore[union-attr]
