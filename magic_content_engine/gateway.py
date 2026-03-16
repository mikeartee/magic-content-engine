"""AgentCore Gateway external tool registration.

Exposes `invoke_content_run` as an MCP tool through AgentCore Gateway
for external callers. Internal Strands tool calls (crawling, scoring,
citation, screenshot, file-writing) use the SDK directly, NOT through
Gateway.

Requirements: REQ-020.1, REQ-020.2
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gateway client protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class GatewayClientProtocol(Protocol):
    """Protocol for AgentCore Gateway client."""

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[..., Any],
    ) -> None:
        """Register an MCP tool with the Gateway."""
        ...


# ---------------------------------------------------------------------------
# MCP tool schema for invoke_content_run
# ---------------------------------------------------------------------------

INVOKE_CONTENT_RUN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {
            "type": "string",
            "enum": ["scheduled", "manual"],
        },
        "run_date": {
            "type": "string",
            "format": "date",
            "description": "YYYY-MM-DD",
        },
        "override_outputs": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Optional output selection override",
        },
    },
    "required": ["source"],
}

# Internal tools that must NOT be registered through Gateway (REQ-020.2).
# These use the Strands SDK directly.
INTERNAL_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "crawl_sources",
        "score_articles",
        "extract_metadata",
        "build_citations",
        "capture_screenshots",
        "write_content",
        "assemble_bundle",
    }
)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register_gateway_tools(client: GatewayClientProtocol) -> None:
    """Register externally accessible MCP tools with the Gateway.

    Only `invoke_content_run` is registered. Internal Strands tool
    calls use the SDK directly and are never exposed via Gateway.
    """
    client.register_tool(
        name="invoke_content_run",
        description=(
            "Trigger a Magic Content Engine run. Accepts a source "
            "(scheduled or manual), optional run date, and optional "
            "output selection override."
        ),
        input_schema=INVOKE_CONTENT_RUN_SCHEMA,
        handler=invoke_content_run_handler,
    )
    logger.info("Registered invoke_content_run with AgentCore Gateway")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def invoke_content_run_handler(params: dict[str, Any]) -> dict[str, Any]:
    """Handle an incoming invoke_content_run MCP tool call.

    Parses params, constructs dependencies, calls run_workflow,
    and returns a summary dict.
    """
    from magic_content_engine.orchestrator import WorkflowDependencies, run_workflow

    source = params["source"]
    run_date_str = params.get("run_date")
    run_date_val = (
        date.fromisoformat(run_date_str) if run_date_str else date.today()
    )

    # In production, dependencies are constructed from AgentCore services.
    # This handler is the integration point; callers must supply deps
    # or a factory must be configured. For now, raise if not wired.
    raise NotImplementedError(
        "Production dependency construction not yet wired. "
        "Use run_workflow() directly with explicit WorkflowDependencies."
    )
