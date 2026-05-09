"""Subeditor Lambda — verdict generation with voice rules check.

Receives a WriterManifest, reads each content file from the output
directory, evaluates it against voice rules and the steering file at
.kiro/steering/01-niche-and-voice.md, and returns a SubeditorReview
with exactly one Verdict per input file.

Verdicts:
  publish — content is ready for publication
  revise  — content needs specific changes (feedback is actionable)
  spike   — content should be discarded (rationale provided)

This agent is strictly read-only. It never writes files.

Requirements: REQ-9.1–REQ-9.6, REQ-10.1–REQ-10.3
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
from datetime import datetime, timezone
from typing import Protocol

from magic_content_engine.bullpen.models import (
    SubeditorReview,
    Verdict,
    WriterManifest,
)
from magic_content_engine.config import SONNET_MODEL_ID, STEERING_BASE_PATH

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Voice rule constants (mirrored from writing_agent.py for standalone use)
# ---------------------------------------------------------------------------

VOICE_BANNED_PHRASES: list[str] = [
    "leverage",
    "empower",
    "unlock",
    "dive into",
    "game-changer",
]

_EM_DASH_RE = re.compile(r"\u2014|&#8212;")
_PARA_OPENS_WITH_I_RE = re.compile(r"(?:^|\n\n)\s*I\b")

# Placeholder format: <!-- MIKE: ... -->
_MIKE_PLACEHOLDER_RE = re.compile(r"<!--\s*MIKE:\s*\[.+?\]\s*-->", re.DOTALL)


# ---------------------------------------------------------------------------
# LLM protocol — testable seam
# ---------------------------------------------------------------------------


class LLMProtocol(Protocol):
    """Protocol for LLM generation calls used by the subeditor."""

    def __call__(self, *, model_id: str, prompt: str) -> str:
        """Generate text from *prompt* using the given *model_id*.

        Returns:
            The generated text (expected to be JSON with verdict fields).

        Raises:
            Exception: On any generation failure.
        """
        ...


# ---------------------------------------------------------------------------
# Voice rule checking
# ---------------------------------------------------------------------------


def check_voice_rules(text: str) -> list[str]:
    """Return a list of voice-rule violations found in *text*.

    Checks:
    - No banned phrases (case-insensitive)
    - No em-dashes (U+2014 or &#8212;)
    - No paragraph/section opening with "I"
    - Proper <!-- MIKE: --> placeholder format (warns on malformed placeholders)

    Returns an empty list when the text passes all checks.
    """
    violations: list[str] = []

    text_lower = text.lower()
    for phrase in VOICE_BANNED_PHRASES:
        if phrase.lower() in text_lower:
            violations.append(f"Banned phrase found: '{phrase}'")

    if _EM_DASH_RE.search(text):
        violations.append("Em-dash character found (use a plain hyphen or rewrite)")

    if _PARA_OPENS_WITH_I_RE.search(text):
        violations.append("Paragraph or section opens with 'I' — rewrite the opening")

    # Check for malformed MIKE placeholders: <!-- MIKE: without proper brackets
    # A valid placeholder looks like: <!-- MIKE: [instruction, ~N words] -->
    # Flag any <!-- MIKE: that doesn't match the canonical format
    raw_mike = re.findall(r"<!--\s*MIKE:[^>]*-->", text, re.DOTALL)
    for candidate in raw_mike:
        if not _MIKE_PLACEHOLDER_RE.match(candidate.strip()):
            violations.append(
                f"Malformed MIKE placeholder — expected <!-- MIKE: [instruction, ~N words] -->: "
                f"{candidate[:80]!r}"
            )

    return violations


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
Role: You are a subeditor and quality reviewer for a content pipeline.

Allowed Actions:
- Read content files from the output/ directory
- Read steering files from .kiro/steering/
- Evaluate content against Voice Rules and output templates

Hard Constraints:
- You MUST NOT write to any file
- You MUST NOT access the web via HTTP
- You MUST NOT access S3
- You MUST NOT send email via SES
- You MUST NOT send messages to any external service
- You MUST NOT modify any content — you review only

Output Format:
Return a JSON object with exactly these fields:
{
  "verdict": "publish" | "revise" | "spike",
  "feedback": "<string — non-empty for revise/spike, empty string for publish>"
}
"""


def _build_review_prompt(
    filename: str,
    content: str,
    voice_violations: list[str],
    voice_rules_text: str,
) -> str:
    """Build the LLM prompt for reviewing a single content file."""
    parts: list[str] = []

    parts.append("## Voice and style rules\n")
    parts.append(voice_rules_text)

    parts.append("\n## Pre-flight voice rule check results\n")
    if voice_violations:
        parts.append(
            f"The following voice rule violations were detected automatically:\n"
        )
        for v in voice_violations:
            parts.append(f"- {v}")
    else:
        parts.append("No automatic voice rule violations detected.")

    parts.append(f"\n## Content file: {filename}\n")
    parts.append("```")
    parts.append(content)
    parts.append("```")

    parts.append(
        "\n## Review instructions\n"
        "Evaluate the content above against the voice rules and output quality standards.\n\n"
        "Return a JSON object with:\n"
        '- "verdict": exactly one of "publish", "revise", or "spike"\n'
        '- "feedback": non-empty string for revise/spike describing what must change '
        "or why the content is being spiked; empty string for publish\n\n"
        "Use 'revise' when the content is salvageable with specific changes.\n"
        "Use 'spike' when the content is fundamentally off-topic, incoherent, "
        "or cannot be fixed with targeted revisions.\n"
        "Use 'publish' when the content meets all voice rules and quality standards.\n\n"
        "If automatic voice rule violations were detected above, the verdict MUST be "
        "'revise' (or 'spike' if the violations are pervasive and unfixable), "
        "and the feedback MUST address each violation specifically.\n\n"
        "Return ONLY the JSON object — no markdown fences, no preamble."
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_VALID_VERDICTS = frozenset({"publish", "revise", "spike"})


def _parse_verdict_response(raw: str, filename: str) -> Verdict:
    """Parse the LLM JSON response into a Verdict.

    Falls back to 'revise' with an error note if the response is malformed,
    so the pipeline always gets a usable verdict rather than crashing.
    """
    # Strip markdown fences if the model wrapped the JSON anyway
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
        cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("Subeditor returned non-JSON for %s: %s", filename, exc)
        return Verdict(
            filename=filename,
            verdict="revise",
            feedback=f"Subeditor response was not valid JSON — manual review required. Raw: {raw[:200]}",
        )

    verdict_str = data.get("verdict", "")
    if verdict_str not in _VALID_VERDICTS:
        logger.warning(
            "Subeditor returned unknown verdict %r for %s", verdict_str, filename
        )
        return Verdict(
            filename=filename,
            verdict="revise",
            feedback=(
                f"Subeditor returned an unrecognised verdict '{verdict_str}' — "
                "manual review required."
            ),
        )

    feedback = str(data.get("feedback", ""))

    # Enforce the contract: revise/spike must have non-empty feedback
    if verdict_str in ("revise", "spike") and not feedback.strip():
        feedback = (
            f"Subeditor issued '{verdict_str}' verdict without feedback — "
            "manual review required."
        )

    return Verdict(filename=filename, verdict=verdict_str, feedback=feedback)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def review(
    manifest: WriterManifest,
    output_dir: str,
    llm: LLMProtocol,
    steering_base_path: str = STEERING_BASE_PATH,
) -> SubeditorReview:
    """Review every file in *manifest* and return a SubeditorReview.

    Steps for each file:
    1. Read the content from *output_dir*.
    2. Run the local voice-rule pre-flight check.
    3. Read the voice rules steering file at runtime.
    4. Call Claude Sonnet with a structured review prompt.
    5. Parse the verdict from the LLM response.

    Returns a SubeditorReview with exactly one Verdict per FileEntry in
    the manifest. Strictly read-only — no files are written.

    Args:
        manifest: The WriterManifest produced by the Writer Agent.
        output_dir: Directory containing the content files to review.
        llm: Callable matching LLMProtocol (injected for testability).
        steering_base_path: Path to the .kiro/steering/ directory.
    """
    output_path = pathlib.Path(output_dir)
    steering_path = pathlib.Path(steering_base_path)
    voice_rules_file = steering_path / "01-niche-and-voice.md"

    # Read voice rules once — fail fast if missing
    if not voice_rules_file.exists():
        raise FileNotFoundError(
            f"Voice rules steering file not found: {voice_rules_file}"
        )
    voice_rules_text = voice_rules_file.read_text(encoding="utf-8")

    verdicts: list[Verdict] = []

    for entry in manifest.files_written:
        filename = entry.path  # preserve full relative path for EIC path resolution
        file_path = output_path / entry.path

        # --- Read content file ---
        if not file_path.exists():
            logger.warning("Content file not found: %s", file_path)
            verdicts.append(
                Verdict(
                    filename=filename,
                    verdict="spike",
                    feedback=f"Content file not found at expected path: {entry.path}",
                )
            )
            continue

        content = file_path.read_text(encoding="utf-8")

        # --- Pre-flight voice rule check ---
        voice_violations = check_voice_rules(content)

        # --- Build prompt and call LLM ---
        prompt = _build_review_prompt(
            filename=filename,
            content=content,
            voice_violations=voice_violations,
            voice_rules_text=voice_rules_text,
        )

        try:
            raw_response = llm(model_id=SONNET_MODEL_ID, prompt=prompt)
        except Exception as exc:
            logger.error("LLM call failed for %s: %s", filename, exc)
            verdicts.append(
                Verdict(
                    filename=filename,
                    verdict="revise",
                    feedback=f"Subeditor LLM call failed — manual review required: {exc}",
                )
            )
            continue

        verdict = _parse_verdict_response(raw_response, filename)
        verdicts.append(verdict)
        logger.info(
            "Subeditor verdict for %s: %s", filename, verdict.verdict
        )

    return SubeditorReview(
        verdicts=verdicts,
        run_timestamp=datetime.now(timezone.utc).isoformat(),
    )
