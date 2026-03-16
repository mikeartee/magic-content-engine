"""Weekly Brief generator — personalised run summary before research crawl.

Generates a WeeklyBrief using Claude Haiku, presenting top performing
content, topic coverage gaps, and a recommended focus topic.

Requirements: REQ-035.1, REQ-035.2, REQ-035.3, REQ-035.4, REQ-035.5,
              REQ-035.6, REQ-035.7
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from magic_content_engine.engagement import identify_top_post
from magic_content_engine.models import PostEngagement, TopicCoverageMap, WeeklyBrief
from magic_content_engine.topic_coverage import derive_recommended_focus, identify_topic_gaps

logger = logging.getLogger(__name__)

# Clean state message shown when no published content exists yet.
CLEAN_STATE_MESSAGE = (
    "No published content yet — engagement tracking will begin after your first post."
)


def generate_weekly_brief(
    coverage_map: TopicCoverageMap,
    engagements: list[PostEngagement],
    run_date: date,
) -> WeeklyBrief:
    """Build a WeeklyBrief from coverage map, engagements, and run date.

    - Uses ``identify_top_post()`` to find the top performing post
      from the past 7 days.
    - Uses ``derive_recommended_focus()`` to pick the best topic
      from gap analysis, optionally weighted by engagement scores.
    - Sets ``clean_state=True`` when no engagements exist.
    """
    clean_state = len(engagements) == 0

    # Top performing post (past 7 days) — None if clean state
    top_post: Optional[PostEngagement] = None
    if not clean_state:
        top_post = identify_top_post(engagements, run_date=run_date)

    # Build engagement scores for focus derivation (topic → total engagement)
    engagement_scores: Optional[dict[str, float]] = None
    if not clean_state:
        # Aggregate engagement by post title as a rough topic proxy.
        # In a full implementation the orchestrator would map posts to topics;
        # here we pass None so derive_recommended_focus uses gap order only.
        engagement_scores = None

    recommended = derive_recommended_focus(coverage_map, engagement_scores)
    # Fallback if every topic is covered and fresh
    if recommended is None:
        recommended = "Kiro IDE"

    brief = WeeklyBrief(
        run_date=run_date,
        top_post=top_post,
        coverage_map=coverage_map,
        recommended_focus=recommended,
        clean_state=clean_state,
    )
    logger.info("Weekly brief generated for %s (clean_state=%s)", run_date, clean_state)
    return brief


def format_weekly_brief(brief: WeeklyBrief) -> str:
    """Format a WeeklyBrief as clean terminal output.

    Output follows the design doc format:

        Weekly brief — YYYY-MM-DD

        Top performing content (past 7 days):
          [title] — [views] views, [reactions] reactions

        Topic coverage map:
          Covered: [topic list with most recent run date]
          Not yet covered: [gap list]

        Recommended focus this week: [topic]

        Press Enter to accept, or type a different topic:
    """
    lines: list[str] = []

    # Header
    lines.append(f"Weekly brief — {brief.run_date.isoformat()}")
    lines.append("")

    # Top performing content
    if brief.clean_state:
        lines.append(f"  {CLEAN_STATE_MESSAGE}")
    elif brief.top_post is not None:
        lines.append("Top performing content (past 7 days):")
        lines.append(
            f"  {brief.top_post.post_title}"
            f" — {brief.top_post.views} views, {brief.top_post.reactions} reactions"
        )
    else:
        lines.append("Top performing content (past 7 days):")
        lines.append("  No posts in the past 7 days.")
    lines.append("")

    # Topic coverage map
    lines.append("Topic coverage map:")

    covered_parts: list[str] = []
    for entry in brief.coverage_map.entries:
        if entry.covered and entry.last_covered_date is not None:
            covered_parts.append(f"{entry.topic} ({entry.last_covered_date.isoformat()})")

    if covered_parts:
        lines.append(f"  Covered: {', '.join(covered_parts)}")
    else:
        lines.append("  Covered: (none)")

    gaps = identify_topic_gaps(brief.coverage_map)
    if gaps:
        lines.append(f"  Not yet covered: {', '.join(gaps)}")
    else:
        lines.append("  Not yet covered: (none)")
    lines.append("")

    # Recommended focus
    lines.append(f"Recommended focus this week: {brief.recommended_focus}")
    lines.append("")
    lines.append("Press Enter to accept, or type a different topic:")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# User focus override (REQ-035.4, REQ-035.5)
# ---------------------------------------------------------------------------


def prompt_user_focus(brief: WeeklyBrief) -> WeeklyBrief:
    """Print the formatted weekly brief and read user focus override.

    Prints the brief to the terminal, then waits for user input:
    - Press Enter to accept the recommended focus (no override).
    - Type a topic to override the recommended focus.

    Returns the updated WeeklyBrief with ``user_override`` set if the
    user typed a topic.
    """
    print(format_weekly_brief(brief))
    user_input = input().strip()
    if user_input:
        brief.user_override = user_input
        logger.info(
            "User overrode recommended focus '%s' with '%s'",
            brief.recommended_focus,
            user_input,
        )
    else:
        logger.info("User accepted recommended focus: %s", brief.recommended_focus)
    return brief


def get_effective_focus(brief: WeeklyBrief) -> str:
    """Return the effective focus topic for scoring.

    Returns ``user_override`` if set, otherwise ``recommended_focus``.
    """
    if brief.user_override:
        return brief.user_override
    return brief.recommended_focus


# ---------------------------------------------------------------------------
# Agent_Log serialisation (REQ-035.6)
# ---------------------------------------------------------------------------


def brief_to_log_dict(brief: WeeklyBrief) -> dict:
    """Serialise a WeeklyBrief for inclusion in the Agent_Log.

    Returns a dict containing run_date, recommended_focus,
    user_override, clean_state, and top_post title (if any).
    """
    result: dict = {
        "run_date": brief.run_date.isoformat(),
        "recommended_focus": brief.recommended_focus,
        "user_override": brief.user_override,
        "clean_state": brief.clean_state,
        "top_post_title": brief.top_post.post_title if brief.top_post else None,
    }
    return result
