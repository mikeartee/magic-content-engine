"""Tests for the output bundle assembler.

Requirements: REQ-017.1–REQ-017.4, REQ-026.1–REQ-026.3
"""

from __future__ import annotations

import json
from datetime import date

from magic_content_engine.bundle import (
    FileOps,
    assemble_bundle,
    format_agent_log,
    format_cost_estimate,
)
from magic_content_engine.models import (
    AgentLog,
    CostEstimate,
    ModelInvocation,
    OutputBundle,
)


# ---------------------------------------------------------------------------
# Fake FileOps for testing
# ---------------------------------------------------------------------------


class FakeFileOps:
    """In-memory file system that satisfies the FileOps protocol."""

    def __init__(self) -> None:
        self.texts: dict[str, str] = {}
        self.jsons: dict[str, dict] = {}
        self.dirs: list[str] = []

    def write_text(self, path: str, content: str) -> None:
        self.texts[path] = content

    def write_json(self, path: str, data: dict) -> None:
        self.jsons[path] = data

    def ensure_dir(self, path: str) -> None:
        self.dirs.append(path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_invocations() -> list[ModelInvocation]:
    return [
        ModelInvocation(
            task_type="relevance_scoring",
            model="claude-haiku",
            input_tokens=500,
            output_tokens=100,
            estimated_cost_usd=0.000150,
        ),
        ModelInvocation(
            task_type="blog_post",
            model="claude-sonnet",
            input_tokens=2000,
            output_tokens=3000,
            estimated_cost_usd=0.025000,
        ),
    ]


def _make_cost_estimate() -> CostEstimate:
    invocations = _make_invocations()
    return CostEstimate(
        invocations=invocations,
        total_llm_cost_usd=0.025150,
        total_agentcore_cost_usd=0.005000,
        total_cost_usd=0.030150,
    )


def _make_agent_log() -> AgentLog:
    return AgentLog(
        run_date="2025-07-14",
        invocation_source="manual",
        articles_found=12,
        articles_kept=5,
        articles=[
            {"url": "https://example.com/a", "score": 4, "status": "confirmed"},
        ],
        model_usage=[
            {"task": "relevance_scoring", "model": "claude-haiku", "tokens": 600},
        ],
        screenshot_results=[
            {"filename": "console-runtime.png", "success": True, "error": None},
        ],
        errors=[],
        selected_outputs=["blog", "youtube"],
        run_metadata={"duration_seconds": 120, "version": "0.1.0"},
    )


def _make_bundle() -> OutputBundle:
    return OutputBundle(
        run_date=date(2025, 7, 14),
        slug="agentcore-browser-launch",
        selected_outputs=["blog", "youtube"],
        generated_files=[],
        references_bib="@online{aws2025, author={AWS}, title={Launch}}",
        cost_estimate=_make_cost_estimate(),
        agent_log=_make_agent_log(),
        s3_key_prefix="output/2025-07-14-agentcore-browser-launch/",
    )


# ---------------------------------------------------------------------------
# format_cost_estimate tests
# ---------------------------------------------------------------------------


class TestFormatCostEstimate:
    def test_produces_readable_text_with_all_fields(self) -> None:
        cost = _make_cost_estimate()
        text = format_cost_estimate(cost)

        assert "Cost Estimate" in text
        assert "Task" in text
        assert "Model" in text
        assert "In Tokens" in text
        assert "Out Tokens" in text
        assert "Cost (USD)" in text

    def test_includes_per_invocation_breakdown(self) -> None:
        cost = _make_cost_estimate()
        text = format_cost_estimate(cost)

        assert "relevance_scoring" in text
        assert "claude-haiku" in text
        assert "blog_post" in text
        assert "claude-sonnet" in text

    def test_includes_totals(self) -> None:
        cost = _make_cost_estimate()
        text = format_cost_estimate(cost)

        assert "LLM cost:" in text
        assert "AgentCore cost:" in text
        assert "Total cost:" in text
        assert "0.025150" in text
        assert "0.005000" in text
        assert "0.030150" in text


# ---------------------------------------------------------------------------
# format_agent_log tests
# ---------------------------------------------------------------------------


class TestFormatAgentLog:
    def test_converts_to_dict_with_all_required_fields(self) -> None:
        log = _make_agent_log()
        result = format_agent_log(log)

        assert isinstance(result, dict)
        assert result["run_date"] == "2025-07-14"
        assert result["invocation_source"] == "manual"
        assert result["articles_found"] == 12
        assert result["articles_kept"] == 5
        assert isinstance(result["articles"], list)
        assert isinstance(result["model_usage"], list)
        assert isinstance(result["screenshot_results"], list)
        assert isinstance(result["errors"], list)
        assert result["selected_outputs"] == ["blog", "youtube"]
        assert isinstance(result["run_metadata"], dict)


# ---------------------------------------------------------------------------
# assemble_bundle tests
# ---------------------------------------------------------------------------


class TestAssembleBundle:
    def test_writes_all_content_files(self) -> None:
        bundle = _make_bundle()
        ops = FakeFileOps()
        content = {"post.md": "# Blog", "script.md": "# Script"}

        assemble_bundle(bundle, content, ops)

        base = "output/2025-07-14-agentcore-browser-launch"
        assert f"{base}/post.md" in ops.texts
        assert f"{base}/script.md" in ops.texts

    def test_always_includes_references_bib(self) -> None:
        bundle = _make_bundle()
        ops = FakeFileOps()

        assemble_bundle(bundle, {}, ops)

        base = "output/2025-07-14-agentcore-browser-launch"
        assert f"{base}/references.bib" in ops.texts
        assert ops.texts[f"{base}/references.bib"] == bundle.references_bib

    def test_always_includes_cost_estimate(self) -> None:
        bundle = _make_bundle()
        ops = FakeFileOps()

        assemble_bundle(bundle, {}, ops)

        base = "output/2025-07-14-agentcore-browser-launch"
        assert f"{base}/cost-estimate.txt" in ops.texts
        assert "Cost Estimate" in ops.texts[f"{base}/cost-estimate.txt"]

    def test_always_includes_agent_log_json(self) -> None:
        bundle = _make_bundle()
        ops = FakeFileOps()

        assemble_bundle(bundle, {}, ops)

        base = "output/2025-07-14-agentcore-browser-launch"
        assert f"{base}/agent-log.json" in ops.jsons
        log_data = ops.jsons[f"{base}/agent-log.json"]
        assert log_data["invocation_source"] == "manual"

    def test_always_creates_screenshots_dir(self) -> None:
        bundle = _make_bundle()
        ops = FakeFileOps()

        assemble_bundle(bundle, {}, ops)

        base = "output/2025-07-14-agentcore-browser-launch"
        assert f"{base}/screenshots" in ops.dirs

    def test_empty_content_files_still_writes_always_included(self) -> None:
        bundle = _make_bundle()
        ops = FakeFileOps()

        paths = assemble_bundle(bundle, {}, ops)

        # No content files, but always-included files are present
        assert len(ops.texts) == 0 or all(
            k.endswith(("references.bib", "cost-estimate.txt"))
            for k in ops.texts
        )
        base = "output/2025-07-14-agentcore-browser-launch"
        assert f"{base}/references.bib" in ops.texts
        assert f"{base}/cost-estimate.txt" in ops.texts
        assert f"{base}/agent-log.json" in ops.jsons

    def test_returns_correct_file_paths(self) -> None:
        bundle = _make_bundle()
        ops = FakeFileOps()
        content = {"post.md": "# Blog"}

        paths = assemble_bundle(bundle, content, ops)

        base = "output/2025-07-14-agentcore-browser-launch"
        assert f"{base}/post.md" in paths
        assert f"{base}/references.bib" in paths
        assert f"{base}/cost-estimate.txt" in paths
        assert f"{base}/agent-log.json" in paths

    def test_returns_all_content_file_paths(self) -> None:
        bundle = _make_bundle()
        ops = FakeFileOps()
        content = {
            "post.md": "# Blog",
            "script.md": "# Script",
            "description.txt": "Description",
        }

        paths = assemble_bundle(bundle, content, ops)

        base = "output/2025-07-14-agentcore-browser-launch"
        # 3 content + 3 always-included = 6
        assert len(paths) == 6
        assert f"{base}/post.md" in paths
        assert f"{base}/script.md" in paths
        assert f"{base}/description.txt" in paths
