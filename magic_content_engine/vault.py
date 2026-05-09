"""Vault integration — read from and write to a local second-brain vault."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class VaultContext:
    permanent_notes: str  # combined text of all 06-permanent/*.md files
    project_note: str     # full text of 01-projects/magic-content-engine.md


class VaultReader:
    def __init__(self, vault_path: str) -> None:
        self._root = Path(vault_path)

    def load(self) -> VaultContext:
        """Read permanent notes and project note from the vault.

        Returns a VaultContext with empty strings for any section that
        cannot be read. Never raises — all errors are logged at WARNING.
        """
        return VaultContext(
            permanent_notes=self._read_permanent_notes(),
            project_note=self._read_project_note(),
        )

    def _read_permanent_notes(self) -> str:
        """Read all .md files directly inside 06-permanent/ (non-recursive).

        Returns combined text separated by double newlines.
        Skips unreadable files with a WARNING log.
        Missing directory returns "" silently.
        """
        permanent_dir = self._root / "06-permanent"
        if not permanent_dir.is_dir():
            return ""

        parts: list[str] = []
        for path in sorted(permanent_dir.iterdir()):
            if path.is_file() and path.suffix == ".md":
                try:
                    with open(path, encoding="utf-8") as fh:
                        parts.append(fh.read())
                except OSError as exc:
                    logger.warning("vault: cannot read %s: %s", path.name, exc)

        return "\n\n".join(parts)

    def _read_project_note(self) -> str:
        """Read 01-projects/magic-content-engine.md.

        Returns empty string and logs WARNING if the file does not exist.
        """
        project_file = self._root / "01-projects" / "magic-content-engine.md"
        if not project_file.exists():
            logger.warning("vault: project note not found at %s", project_file)
            return ""
        with open(project_file, encoding="utf-8") as fh:
            return fh.read()


class VaultWriter:
    def __init__(self, vault_path: str) -> None:
        self._root = Path(vault_path)

    def write_summary(
        self,
        *,
        run_date,
        articles_found: int,
        articles_kept: int,
        confirmed_titles: list[str],
        selected_outputs: list[str],
        gate_decisions: list[dict],
        errors: list[dict],
    ) -> None:
        """Write MCE-run-YYYY-MM-DD.md to 00-inbox/.

        Creates 00-inbox/ if it does not exist.
        Overwrites any existing file with the same name.
        Logs at ERROR and returns silently on any filesystem failure.
        """
        inbox_dir = self._root / "00-inbox"
        try:
            inbox_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("vault: failed to create inbox dir %s: %s", inbox_dir, exc)
            return

        filename = f"MCE-run-{run_date.isoformat()}.md"
        dest = inbox_dir / filename
        content = _build_summary_markdown(
            run_date,
            articles_found,
            articles_kept,
            confirmed_titles,
            selected_outputs,
            gate_decisions,
            errors,
        )
        try:
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            logger.error("vault: failed to write run summary to %s: %s", dest, exc)


def _build_summary_markdown(
    run_date,
    articles_found: int,
    articles_kept: int,
    confirmed_titles: list[str],
    selected_outputs: list[str],
    gate_decisions: list[dict],
    errors: list[dict],
) -> str:
    lines: list[str] = []

    lines.append(f"# Magic Content Engine — Run {run_date.isoformat()}")
    lines.append("")

    lines.append("## Articles scored")
    lines.append(f"Found: {articles_found}  Kept: {articles_kept}")
    lines.append("")

    lines.append("## Topics covered")
    if confirmed_titles:
        for title in confirmed_titles:
            lines.append(f"- {title}")
    else:
        lines.append("None")
    lines.append("")

    lines.append("## Outputs generated")
    if selected_outputs:
        for output in selected_outputs:
            lines.append(f"- {output}")
    else:
        lines.append("None")
    lines.append("")

    lines.append("## Publish gate decisions")
    if gate_decisions:
        for decision in gate_decisions:
            lines.append(f"- {decision['filename']}: {decision['decision']}")
    else:
        lines.append("None")
    lines.append("")

    lines.append("## Errors")
    if errors:
        for error in errors:
            lines.append(f"- {error}")
    else:
        lines.append("None")

    return "\n".join(lines)
