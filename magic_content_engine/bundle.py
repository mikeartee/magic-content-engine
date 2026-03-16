"""Output bundle assembler.

Assembles the final output directory with selected content files and
always-included files (references.bib, cost-estimate.txt, screenshots/,
agent-log.json).

Requirements: REQ-017.1–REQ-017.4, REQ-026.1–REQ-026.3
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date
from typing import Any, Protocol

from magic_content_engine.models import AgentLog, CostEstimate, ModelInvocation, OutputBundle

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File operations protocol — testable seam
# ---------------------------------------------------------------------------


class FileOps(Protocol):
    """Protocol for file system operations.

    Any object matching this interface can be injected, making the
    bundle assembler testable without touching the real file system.
    """

    def write_text(self, path: str, content: str) -> None:
        """Write *content* as UTF-8 text to *path*."""
        ...

    def write_json(self, path: str, data: dict) -> None:
        """Serialise *data* as JSON and write to *path*."""
        ...

    def ensure_dir(self, path: str) -> None:
        """Create *path* (and parents) if it does not exist."""
        ...


# ---------------------------------------------------------------------------
# Cost estimate formatting
# ---------------------------------------------------------------------------


def format_cost_estimate(cost: CostEstimate) -> str:
    """Format a ``CostEstimate`` as a human-readable plain-text report.

    Produces a per-invocation breakdown table followed by totals.
    """
    lines: list[str] = []
    lines.append("Cost Estimate")
    lines.append("=" * 72)
    lines.append("")

    # Header
    header = f"{'Task':<30} {'Model':<16} {'In Tokens':>10} {'Out Tokens':>11} {'Cost (USD)':>12}"
    lines.append(header)
    lines.append("-" * 72)

    for inv in cost.invocations:
        row = (
            f"{inv.task_type:<30} "
            f"{inv.model:<16} "
            f"{inv.input_tokens:>10,} "
            f"{inv.output_tokens:>11,} "
            f"${inv.estimated_cost_usd:>10.6f}"
        )
        lines.append(row)

    lines.append("-" * 72)
    lines.append("")
    lines.append(f"LLM cost:       ${cost.total_llm_cost_usd:.6f}")
    lines.append(f"AgentCore cost: ${cost.total_agentcore_cost_usd:.6f}")
    lines.append(f"Total cost:     ${cost.total_cost_usd:.6f}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent log formatting
# ---------------------------------------------------------------------------


def _serialise_date(obj: Any) -> Any:
    """JSON-safe conversion for date objects."""
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def format_agent_log(log: AgentLog) -> dict:
    """Convert an ``AgentLog`` dataclass to a JSON-serialisable dict."""
    return asdict(log)


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------


def assemble_bundle(
    bundle: OutputBundle,
    content_files: dict[str, str],
    file_ops: FileOps,
) -> list[str]:
    """Assemble the output bundle directory.

    Creates ``output/YYYY-MM-DD-[slug]/`` and writes:
    - Each entry in *content_files* (key = filename, value = content)
    - ``references.bib`` (always)
    - ``cost-estimate.txt`` (always)
    - ``agent-log.json`` (always)
    - ``screenshots/`` directory (always, created empty)

    Returns the list of all written file paths (relative to the bundle
    root).
    """
    base_dir = f"output/{bundle.run_date.isoformat()}-{bundle.slug}"
    file_ops.ensure_dir(base_dir)

    written: list[str] = []

    # Selected content files
    for filename, content in content_files.items():
        path = f"{base_dir}/{filename}"
        file_ops.write_text(path, content)
        written.append(path)

    # Always-included: references.bib
    refs_path = f"{base_dir}/references.bib"
    file_ops.write_text(refs_path, bundle.references_bib)
    written.append(refs_path)

    # Always-included: cost-estimate.txt
    cost_path = f"{base_dir}/cost-estimate.txt"
    file_ops.write_text(cost_path, format_cost_estimate(bundle.cost_estimate))
    written.append(cost_path)

    # Always-included: screenshots/ directory
    screenshots_dir = f"{base_dir}/screenshots"
    file_ops.ensure_dir(screenshots_dir)

    # Always-included: agent-log.json
    log_path = f"{base_dir}/agent-log.json"
    file_ops.write_json(log_path, format_agent_log(bundle.agent_log))
    written.append(log_path)

    return written
