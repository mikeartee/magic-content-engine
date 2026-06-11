"""
Agent callable factory for the GUI pipeline runner.

Thin wrapper over the shared wiring factory
(``magic_content_engine.bullpen.wiring.build_agent_callables``). The GUI
supplies its own ``threading.Event``-based approval gate from
``scripts/gui/pipeline_runner._make_approval_fn`` and passes it into
``run_pipeline()`` separately, so this factory returns the agent callables
*without* an ``approval_fn`` key (the historical GUI contract).

This module is imported lazily inside the pipeline thread to keep AWS client
construction off the critical path at server startup.
"""

from __future__ import annotations

import os

from magic_content_engine.bullpen.models import BullpenBrief, SubeditorReview
from magic_content_engine.bullpen.wiring import build_agent_callables as _build_shared


def _noop_approval_fn(sub_review: "SubeditorReview | None") -> bool:
    """Placeholder gate. The GUI never uses this — ``pipeline_runner`` supplies
    its own ``threading.Event`` gate and passes it to ``run_pipeline()``
    directly. It exists only to satisfy the shared factory's required
    ``approval_fn`` parameter; it is stripped from the returned dict below."""
    return True


def build_agent_callables(run_state: object, brief: BullpenBrief) -> dict:
    """Build all agent callables for a pipeline run.

    Returns a dict suitable for ``**kwargs`` unpacking into ``run_pipeline()``,
    *excluding* ``approval_fn`` (the GUI provides its own event-based gate).
    """
    output_dir: str = getattr(run_state, "output_dir", "output")
    github_token: str | None = os.getenv("GITHUB_TOKEN")

    callables = _build_shared(
        brief=brief,
        output_dir=output_dir,
        approval_fn=_noop_approval_fn,
        github_token=github_token,
    )
    # The GUI supplies its own threading.Event approval gate via
    # pipeline_runner; drop the placeholder so the historical 7-key contract
    # is preserved and there is no duplicate approval_fn kwarg downstream.
    callables.pop("approval_fn", None)
    return callables
