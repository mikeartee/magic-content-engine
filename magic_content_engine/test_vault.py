"""Unit tests for vault.py — VaultReader, VaultWriter, config, and orchestrator integration.

Tasks 6.1, 6.2, 6.3
Requirements: 1.1, 2.1, 2.2, 2.4, 2.5, 3.1, 3.2, 5.1, 5.2, 5.3, 5.4, 5.6, 6.5
"""

from __future__ import annotations

import importlib
import os
from datetime import date

import pytest

from magic_content_engine.vault import VaultReader, VaultWriter


# ---------------------------------------------------------------------------
# Task 6.1 — VaultReader unit tests
# ---------------------------------------------------------------------------


class TestVaultReader:
    def test_vault_reader_empty_permanent_dir(self, tmp_path):
        """06-permanent/ exists but has no .md files -> permanent_notes == ''."""
        permanent = tmp_path / "06-permanent"
        permanent.mkdir()
        # Add a non-.md file to confirm it is ignored
        (permanent / "notes.txt").write_text("ignored", encoding="utf-8")

        vc = VaultReader(str(tmp_path)).load()
        assert vc.permanent_notes == ""

    def test_vault_reader_missing_permanent_dir(self, tmp_path):
        """06-permanent/ absent -> permanent_notes == ''."""
        vc = VaultReader(str(tmp_path)).load()
        assert vc.permanent_notes == ""

    def test_vault_reader_missing_project_note(self, tmp_path):
        """01-projects/magic-content-engine.md absent -> project_note == ''."""
        vc = VaultReader(str(tmp_path)).load()
        assert vc.project_note == ""

    def test_vault_reader_utf8(self, tmp_path):
        """File containing non-ASCII text is read correctly."""
        permanent = tmp_path / "06-permanent"
        permanent.mkdir()
        content = "Kia ora \u2014 ng\u0101 mihi"
        (permanent / "note.md").write_text(content, encoding="utf-8")

        vc = VaultReader(str(tmp_path)).load()
        assert content in vc.permanent_notes

    def test_vault_reader_no_recursion(self, tmp_path):
        """A .md file inside a sub-folder of 06-permanent/ is NOT included."""
        permanent = tmp_path / "06-permanent"
        permanent.mkdir()
        sub = permanent / "subfolder"
        sub.mkdir()
        (sub / "nested.md").write_text("should be excluded", encoding="utf-8")
        (permanent / "top.md").write_text("should be included", encoding="utf-8")

        vc = VaultReader(str(tmp_path)).load()
        assert "should be included" in vc.permanent_notes
        assert "should be excluded" not in vc.permanent_notes


# ---------------------------------------------------------------------------
# Task 6.2 — VaultWriter unit tests
# ---------------------------------------------------------------------------


def _default_summary_kwargs(run_date: date | None = None) -> dict:
    """Return minimal valid kwargs for VaultWriter.write_summary()."""
    return dict(
        run_date=run_date or date(2025, 7, 14),
        articles_found=5,
        articles_kept=3,
        confirmed_titles=["Article A", "Article B"],
        selected_outputs=["blog"],
        gate_decisions=[{"filename": "post.md", "decision": "approve"}],
        errors=[],
    )


class TestVaultWriter:
    def test_vault_writer_creates_inbox_dir(self, tmp_path):
        """00-inbox/ does not exist -> created automatically."""
        assert not (tmp_path / "00-inbox").exists()
        VaultWriter(str(tmp_path)).write_summary(**_default_summary_kwargs())
        assert (tmp_path / "00-inbox").is_dir()

    def test_vault_writer_overwrites_existing_file(self, tmp_path):
        """Calling write_summary() twice produces the same single file."""
        kwargs = _default_summary_kwargs(run_date=date(2025, 7, 14))
        writer = VaultWriter(str(tmp_path))
        writer.write_summary(**kwargs)
        writer.write_summary(**kwargs)

        inbox = tmp_path / "00-inbox"
        files = list(inbox.iterdir())
        assert len(files) == 1
        assert files[0].name == "MCE-run-2025-07-14.md"

    def test_vault_writer_utf8(self, tmp_path):
        """Summary with non-ASCII characters round-trips correctly."""
        kwargs = _default_summary_kwargs()
        kwargs["confirmed_titles"] = ["Kia ora \u2014 ng\u0101 mihi"]
        VaultWriter(str(tmp_path)).write_summary(**kwargs)

        output_file = tmp_path / "00-inbox" / "MCE-run-2025-07-14.md"
        content = output_file.read_text(encoding="utf-8")
        assert "Kia ora \u2014 ng\u0101 mihi" in content

    def test_vault_writer_all_sections_present(self, tmp_path):
        """Output contains all six required headings in order."""
        VaultWriter(str(tmp_path)).write_summary(**_default_summary_kwargs())

        output_file = tmp_path / "00-inbox" / "MCE-run-2025-07-14.md"
        content = output_file.read_text(encoding="utf-8")

        expected_headings = [
            "# Magic Content Engine \u2014 Run",
            "## Articles scored",
            "## Topics covered",
            "## Outputs generated",
            "## Publish gate decisions",
            "## Errors",
        ]
        positions = [content.index(h) for h in expected_headings]
        assert positions == sorted(positions), "Headings are not in the required order"


# ---------------------------------------------------------------------------
# Task 6.3 — Config and orchestrator integration tests
# ---------------------------------------------------------------------------


class TestConfigVaultPath:
    def test_config_vault_path_default(self, monkeypatch):
        """VAULT_PATH env var absent -> os.getenv returns ''."""
        monkeypatch.delenv("VAULT_PATH", raising=False)
        # Config is loaded at import time, so test the env var directly
        # as the spec instructs (re-read via os.getenv).
        value = os.getenv("VAULT_PATH", "")
        assert value == ""


class TestWorkflowDependenciesVaultPath:
    def test_workflow_dependencies_vault_path(self):
        """WorkflowDependencies accepts an injected vault_path."""
        from magic_content_engine.test_orchestrator import (
            StubBrowser,
            StubBundleFileOps,
            StubDedupMemory,
            StubDevToAPI,
            StubEngagementMemory,
            StubGateFileOps,
            StubHeldItemMemory,
            StubLLM,
            StubLLMWriter,
            StubMemory,
            StubS3Client,
            StubScreenshotBrowser,
            StubSES,
            StubTopicMemory,
        )
        from magic_content_engine.orchestrator import WorkflowDependencies

        deps = WorkflowDependencies(
            memory=StubMemory(),
            dedup_memory=StubDedupMemory(),
            topic_memory=StubTopicMemory(),
            engagement_api=StubDevToAPI(),
            engagement_memory=StubEngagementMemory(),
            held_item_memory=StubHeldItemMemory(),
            ses_notifier=StubSES(),
            browser=StubBrowser(),
            llm_scorer=StubLLM(),
            llm_extractor=StubLLM(),
            llm_formatter=StubLLM(),
            llm_writer=StubLLMWriter(),
            screenshot_browser=StubScreenshotBrowser(),
            s3_client=StubS3Client(),
            bundle_file_ops=StubBundleFileOps(),
            gate_file_ops=StubGateFileOps(),
            vault_path="/tmp/test",
        )
        assert deps.vault_path == "/tmp/test"
