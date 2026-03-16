"""Tests for AgentCore Gateway external tool registration.

Requirements: REQ-020.1, REQ-020.2
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from magic_content_engine.gateway import (
    INTERNAL_ONLY_TOOLS,
    INVOKE_CONTENT_RUN_SCHEMA,
    GatewayClientProtocol,
    invoke_content_run_handler,
    register_gateway_tools,
)


# ---------------------------------------------------------------------------
# Stub Gateway client
# ---------------------------------------------------------------------------


class StubGatewayClient:
    """Records register_tool calls for assertion."""

    def __init__(self) -> None:
        self.registered: list[dict[str, Any]] = []

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[..., Any],
    ) -> None:
        self.registered.append(
            {
                "name": name,
                "description": description,
                "input_schema": input_schema,
                "handler": handler,
            }
        )


# ---------------------------------------------------------------------------
# INVOKE_CONTENT_RUN_SCHEMA validation (REQ-020.1)
# ---------------------------------------------------------------------------


class TestInvokeContentRunSchema:
    def test_schema_is_object_type(self):
        assert INVOKE_CONTENT_RUN_SCHEMA["type"] == "object"

    def test_schema_has_source_property(self):
        props = INVOKE_CONTENT_RUN_SCHEMA["properties"]
        assert "source" in props
        assert props["source"]["type"] == "string"
        assert set(props["source"]["enum"]) == {"scheduled", "manual"}

    def test_schema_has_run_date_property(self):
        props = INVOKE_CONTENT_RUN_SCHEMA["properties"]
        assert "run_date" in props
        assert props["run_date"]["type"] == "string"
        assert props["run_date"]["format"] == "date"

    def test_schema_has_override_outputs_property(self):
        props = INVOKE_CONTENT_RUN_SCHEMA["properties"]
        assert "override_outputs" in props
        assert props["override_outputs"]["type"] == "array"

    def test_source_is_required(self):
        assert "source" in INVOKE_CONTENT_RUN_SCHEMA["required"]

    def test_run_date_is_not_required(self):
        assert "run_date" not in INVOKE_CONTENT_RUN_SCHEMA["required"]

    def test_override_outputs_is_not_required(self):
        assert "override_outputs" not in INVOKE_CONTENT_RUN_SCHEMA["required"]


# ---------------------------------------------------------------------------
# register_gateway_tools (REQ-020.1)
# ---------------------------------------------------------------------------


class TestRegisterGatewayTools:
    def test_registers_invoke_content_run(self):
        client = StubGatewayClient()
        register_gateway_tools(client)

        assert len(client.registered) == 1
        assert client.registered[0]["name"] == "invoke_content_run"

    def test_registered_schema_matches(self):
        client = StubGatewayClient()
        register_gateway_tools(client)

        assert client.registered[0]["input_schema"] is INVOKE_CONTENT_RUN_SCHEMA

    def test_registered_handler_is_callable(self):
        client = StubGatewayClient()
        register_gateway_tools(client)

        assert callable(client.registered[0]["handler"])

    def test_registered_handler_is_invoke_content_run_handler(self):
        client = StubGatewayClient()
        register_gateway_tools(client)

        assert client.registered[0]["handler"] is invoke_content_run_handler

    def test_client_satisfies_protocol(self):
        client = StubGatewayClient()
        assert isinstance(client, GatewayClientProtocol)

    def test_only_one_tool_registered(self):
        """Only invoke_content_run should be registered, not internal tools."""
        client = StubGatewayClient()
        register_gateway_tools(client)

        registered_names = {r["name"] for r in client.registered}
        assert registered_names == {"invoke_content_run"}


# ---------------------------------------------------------------------------
# Internal tools NOT registered through Gateway (REQ-020.2)
# ---------------------------------------------------------------------------


class TestInternalToolsNotRegistered:
    def test_internal_tools_not_in_gateway(self):
        """Internal Strands tool calls must NOT be registered via Gateway."""
        client = StubGatewayClient()
        register_gateway_tools(client)

        registered_names = {r["name"] for r in client.registered}
        for tool in INTERNAL_ONLY_TOOLS:
            assert tool not in registered_names, (
                f"Internal tool '{tool}' should not be registered through Gateway"
            )

    def test_internal_tools_list_covers_key_operations(self):
        expected = {
            "crawl_sources",
            "score_articles",
            "extract_metadata",
            "build_citations",
            "capture_screenshots",
            "write_content",
            "assemble_bundle",
        }
        assert INTERNAL_ONLY_TOOLS == expected


# ---------------------------------------------------------------------------
# invoke_content_run_handler returns expected structure (REQ-020.1)
# ---------------------------------------------------------------------------


class TestInvokeContentRunHandler:
    def test_handler_raises_not_implemented(self):
        """Handler raises until production deps are wired."""
        with pytest.raises(NotImplementedError, match="dependency construction"):
            invoke_content_run_handler({"source": "manual"})
