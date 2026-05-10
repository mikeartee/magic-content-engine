"""
Bullpen Web GUI — pipeline background thread and approval gate.

This module is responsible for:
- Running ``run_pipeline()`` from ``magic_content_engine.bullpen.editor_in_chief``
  in a background thread so the Flask server stays responsive.
- Providing ``_make_approval_fn``, which replaces the terminal ``input()`` gate
  in ``run_local.py`` with a ``threading.Event`` that the GUI signals via
  ``POST /api/run/approve`` or ``POST /api/run/reject``.

Implemented in Task 3.
"""

# Standard library
import threading

# Pipeline imports (not modified — read-only consumers)
# from magic_content_engine.bullpen.editor_in_chief import run_pipeline
# from magic_content_engine.bullpen.models import BullpenBrief
