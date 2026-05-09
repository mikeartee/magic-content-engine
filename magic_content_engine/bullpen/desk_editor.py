"""Desk Editor Lambda — ResearchBrief to ContentBrief.

Receives a ResearchBrief and a topic string, reads the voice/niche
steering file at runtime, selects the most relevant articles for the
topic and Aotearoa builder perspective, determines the editorial angle
and tone guidance, and returns a ContentBrief.

No web access. No S3 access. Uses Claude Sonnet.

Requirements: REQ-5, REQ-6 (bullpen-architecture spec)
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone

import boto3

from magic_content_engine.config import SONNET_MODEL_ID, STEERING_BASE_PATH
from magic_content_engine.bullpen.models import ContentBrief, ResearchBrief, ScoredArticle

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
Role: You are a desk editor for a weekly content pipeline. You read research \
briefs and produce content briefs with editorial angle, tone guidance, and \
article selection.

Allowed Actions:
- Read steering files from .kiro/steering/
- Structure content briefs using Claude Sonnet
- Select articles from the Research Brief for content production

Hard Constraints:
- You MUST NOT access the web via HTTP
- You MUST NOT access S3
- You MUST NOT send email via SES
- You MUST NOT write files outside the Content Brief output
- You MUST NOT send messages to any external service

Input/Output Format:
- Input: JSON with fields "research_brief" (ResearchBrief object), \
"topic" (string from Weekly Brief), "voice_rules" (string from steering file)
- Output: JSON object with fields "selected_articles" (list of article objects \
chosen for content), "editorial_angle" (string), "tone_guidance" (string \
referencing voice rules), "output_types" (list of requested output type strings)
"""

# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_user_prompt(
    research_brief: ResearchBrief,
    topic: str,
    voice_rules: str,
    output_types: list[str],
) -> str:
    """Build the user-turn prompt for the desk editor."""
    articles_json = json.dumps(
        [
            {
                "title": a.title,
                "url": a.url,
                "source": a.source,
                "relevance_score": a.relevance_score,
                "summary": a.summary,
            }
            for a in research_brief.articles
        ],
        indent=2,
    )

    output_types_str = ", ".join(output_types) if output_types else "blog"

    return f"""\
You are editing a weekly content brief for an AWS Community Builder in \
Aotearoa New Zealand. The topic this week is: {topic!r}

Voice and niche rules (from 01-niche-and-voice.md):
{voice_rules}

Research articles available (scored 1-5 for relevance):
{articles_json}

Requested output types: {output_types_str}

Your task:
1. Select the most relevant articles for the topic and the Aotearoa builder \
perspective. Prefer articles with higher relevance scores. Select between 2 \
and 5 articles.
2. Write a concise editorial_angle (1-2 sentences) that frames the story for \
the Aotearoa AWS community. Be specific to the topic.
3. Write tone_guidance (2-3 sentences) that references the voice rules above. \
Mention at least one specific voice rule by name (e.g. "no em-dashes", \
"short sentences", "no banned phrases").
4. Return the output_types list as provided.

Respond with a single JSON object — no markdown fences, no extra text:
{{
  "selected_articles": [
    {{
      "title": "...",
      "url": "...",
      "source": "...",
      "relevance_score": <int>,
      "summary": "..."
    }}
  ],
  "editorial_angle": "...",
  "tone_guidance": "...",
  "output_types": ["{output_types_str.replace(', ', '", "')}"]
}}
"""


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def _parse_response(raw: str) -> dict:
    """Parse the LLM JSON response, stripping any markdown fences."""
    text = raw.strip()
    # Strip ```json ... ``` or ``` ... ``` fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop first line (```json or ```) and last line (```)
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Desk editor response is not valid JSON: {exc}\nRaw: {raw!r}") from exc


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------


def run_desk_editor(
    research_brief: ResearchBrief,
    topic: str,
    output_types: list[str] | None = None,
    steering_base_path: str = STEERING_BASE_PATH,
    bedrock_client=None,
    model_id: str = SONNET_MODEL_ID,
) -> ContentBrief:
    """Transform a ResearchBrief into a ContentBrief.

    Args:
        research_brief: Output from the Researcher Agent.
        topic: The topic string from the BullpenBrief.
        output_types: Requested output types (e.g. ["blog", "youtube"]).
            Defaults to ["blog"] if not provided.
        steering_base_path: Directory containing steering markdown files.
            Read at runtime — never cached.
        bedrock_client: Optional pre-built boto3 bedrock-runtime client.
            Created automatically if not provided.
        model_id: Bedrock model ID to use. Defaults to SONNET_MODEL_ID.

    Returns:
        A JSON-serialisable ContentBrief.

    Raises:
        FileNotFoundError: If the voice steering file is missing.
        ValueError: If the LLM response cannot be parsed.
    """
    if output_types is None:
        output_types = ["blog"]

    # Read steering file at runtime
    voice_path = pathlib.Path(steering_base_path) / "01-niche-and-voice.md"
    if not voice_path.exists():
        raise FileNotFoundError(f"Voice steering file missing: {voice_path}")
    voice_rules = voice_path.read_text(encoding="utf-8")

    logger.info(
        "Desk editor starting — topic=%r, articles=%d, output_types=%s",
        topic,
        len(research_brief.articles),
        output_types,
    )

    # Build prompt
    user_prompt = _build_user_prompt(research_brief, topic, voice_rules, output_types)

    # Invoke Bedrock
    if bedrock_client is None:
        bedrock_client = boto3.client("bedrock-runtime")

    request_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    response = bedrock_client.invoke_model(
        modelId=model_id,
        body=json.dumps(request_body),
        contentType="application/json",
        accept="application/json",
    )

    response_body = json.loads(response["body"].read())
    raw_text = response_body["content"][0]["text"]

    logger.debug("Desk editor raw response: %s", raw_text[:500])

    # Parse response
    parsed = _parse_response(raw_text)

    # Build ScoredArticle list from response
    selected_articles = [
        ScoredArticle(
            title=a["title"],
            url=a["url"],
            source=a["source"],
            relevance_score=int(a["relevance_score"]),
            summary=a["summary"],
        )
        for a in parsed.get("selected_articles", [])
    ]

    editorial_angle = parsed.get("editorial_angle", "")
    tone_guidance = parsed.get("tone_guidance", "")
    returned_output_types = parsed.get("output_types", output_types)

    if not editorial_angle:
        raise ValueError("Desk editor returned empty editorial_angle")
    if not tone_guidance:
        raise ValueError("Desk editor returned empty tone_guidance")

    run_timestamp = datetime.now(timezone.utc).isoformat()

    brief = ContentBrief(
        selected_articles=selected_articles,
        editorial_angle=editorial_angle,
        tone_guidance=tone_guidance,
        output_types=returned_output_types,
        run_timestamp=run_timestamp,
    )

    logger.info(
        "Desk editor complete — selected=%d articles, angle=%r",
        len(selected_articles),
        editorial_angle[:80],
    )

    return brief


# ---------------------------------------------------------------------------
# Lambda handler entry point
# ---------------------------------------------------------------------------


def handler(event: dict, context=None) -> dict:
    """AWS Lambda handler for the Desk Editor.

    Expected event shape:
        {
            "research_brief": { ... ResearchBrief fields ... },
            "topic": "string",
            "output_types": ["blog", "youtube"]  // optional
        }

    Returns the ContentBrief as a JSON-serialisable dict.
    """
    # Deserialise ResearchBrief from event
    rb_data = event["research_brief"]
    articles = [
        ScoredArticle(
            title=a["title"],
            url=a["url"],
            source=a["source"],
            relevance_score=int(a["relevance_score"]),
            summary=a["summary"],
        )
        for a in rb_data.get("articles", [])
    ]
    research_brief = ResearchBrief(
        articles=articles,
        sources_crawled=rb_data.get("sources_crawled", []),
        sources_failed=rb_data.get("sources_failed", []),
        run_timestamp=rb_data.get("run_timestamp", ""),
    )

    topic = event["topic"]
    output_types = event.get("output_types", ["blog"])

    brief = run_desk_editor(research_brief, topic, output_types=output_types)

    # Serialise to dict for Lambda response
    return {
        "selected_articles": [
            {
                "title": a.title,
                "url": a.url,
                "source": a.source,
                "relevance_score": a.relevance_score,
                "summary": a.summary,
            }
            for a in brief.selected_articles
        ],
        "editorial_angle": brief.editorial_angle,
        "tone_guidance": brief.tone_guidance,
        "output_types": brief.output_types,
        "run_timestamp": brief.run_timestamp,
    }
