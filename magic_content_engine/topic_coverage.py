"""Topic Coverage Map persistence and gap analysis.

Tracks which niche topics have been covered across runs, identifies
gaps, and derives a recommended focus topic based on natural topic
progression and engagement signals.

The actual AgentCore Memory integration is wired in by the
orchestrator; this module depends only on the
``TopicCoverageMemoryProtocol`` interface so that tests can supply
a lightweight stub.

Requirements: REQ-033.1, REQ-033.2, REQ-033.3, REQ-033.4, REQ-033.5, REQ-033.6
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional, Protocol, runtime_checkable

from magic_content_engine.models import TopicCoverageEntry, TopicCoverageMap

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Niche topics and adjacency chains
# ---------------------------------------------------------------------------

NICHE_TOPICS: list[str] = [
    "Kiro IDE",
    "AgentCore Runtime",
    "AgentCore Gateway",
    "AgentCore Browser",
    "AgentCore Memory",
    "AgentCore Identity",
    "AgentCore Observability",
    "Strands Agents SDK",
    "Bedrock",
    "MCP Protocol",
    "AWS Community Builders",
    "kiro-steering-docs-extension",
]

# Adjacent topic chains: natural progression through AgentCore services.
# Key = topic, Value = list of natural next topics.
ADJACENT_TOPICS: dict[str, list[str]] = {
    "AgentCore Runtime": ["AgentCore Gateway"],
    "AgentCore Gateway": ["AgentCore Browser"],
    "AgentCore Browser": ["AgentCore Memory"],
    "AgentCore Memory": ["AgentCore Identity"],
    "AgentCore Identity": ["AgentCore Observability"],
    "AgentCore Observability": ["AgentCore Runtime"],
}


# ---------------------------------------------------------------------------
# Memory abstraction
# ---------------------------------------------------------------------------


@runtime_checkable
class TopicCoverageMemoryProtocol(Protocol):
    """Dependency-injection interface for topic coverage persistence.

    In production this is backed by AgentCore Memory (long-term).
    Tests can supply a lightweight stub.
    """

    def load_topic_coverage_map(self) -> Optional[TopicCoverageMap]:
        """Load the persisted TopicCoverageMap, or None if not yet stored."""
        ...

    def save_topic_coverage_map(self, coverage_map: TopicCoverageMap) -> None:
        """Persist the TopicCoverageMap to long-term memory."""
        ...


# ---------------------------------------------------------------------------
# Initialisation — first run handling (REQ-033.1)
# ---------------------------------------------------------------------------


def _build_initial_entry(topic: str) -> TopicCoverageEntry:
    """Create an uncovered entry for a niche topic with adjacency info."""
    return TopicCoverageEntry(
        topic=topic,
        covered=False,
        article_titles=[],
        last_covered_date=None,
        adjacent_topics=ADJACENT_TOPICS.get(topic, []),
    )


def create_empty_coverage_map(run_date: date) -> TopicCoverageMap:
    """Build a fresh TopicCoverageMap with all niche topics uncovered."""
    entries = [_build_initial_entry(topic) for topic in NICHE_TOPICS]
    return TopicCoverageMap(entries=entries, last_updated=run_date)


def load_or_create_coverage_map(
    memory: TopicCoverageMemoryProtocol,
    run_date: date,
) -> TopicCoverageMap:
    """Load the coverage map from memory, or create an empty one on first run.

    On first run (empty map), all niche topics are listed as uncovered.
    """
    existing = memory.load_topic_coverage_map()
    if existing is not None:
        logger.info("Loaded existing TopicCoverageMap with %d entries", len(existing.entries))
        return existing

    logger.info("No existing TopicCoverageMap found — creating empty map (first run)")
    empty_map = create_empty_coverage_map(run_date)
    return empty_map


# ---------------------------------------------------------------------------
# Gap identification (REQ-033.3)
# ---------------------------------------------------------------------------


def identify_topic_gaps(
    coverage_map: TopicCoverageMap,
    staleness_days: int = 30,
) -> list[str]:
    """Return topics that are uncovered or stale.

    A topic is a gap if:
    - ``covered`` is False, OR
    - ``last_covered_date`` is older than *staleness_days* from today

    Returns topic names sorted with fully uncovered topics first,
    then stale topics ordered by oldest coverage date.
    """
    uncovered: list[str] = []
    stale: list[tuple[str, date]] = []
    today = date.today()

    for entry in coverage_map.entries:
        if not entry.covered:
            uncovered.append(entry.topic)
        elif entry.last_covered_date is not None:
            age = (today - entry.last_covered_date).days
            if age > staleness_days:
                stale.append((entry.topic, entry.last_covered_date))

    # Sort stale by oldest first
    stale.sort(key=lambda t: t[1])
    return uncovered + [topic for topic, _ in stale]


# ---------------------------------------------------------------------------
# Recommended focus derivation (REQ-033.4)
# ---------------------------------------------------------------------------


def _find_entry(coverage_map: TopicCoverageMap, topic: str) -> Optional[TopicCoverageEntry]:
    """Find an entry by topic name."""
    for entry in coverage_map.entries:
        if entry.topic == topic:
            return entry
    return None


def _recently_covered_topics(coverage_map: TopicCoverageMap) -> list[TopicCoverageEntry]:
    """Return covered entries sorted by most recently covered first."""
    covered = [e for e in coverage_map.entries if e.covered and e.last_covered_date is not None]
    covered.sort(key=lambda e: e.last_covered_date, reverse=True)  # type: ignore[arg-type]
    return covered


def derive_recommended_focus(
    coverage_map: TopicCoverageMap,
    engagement_scores: Optional[dict[str, float]] = None,
) -> Optional[str]:
    """Pick the best uncovered topic based on gap analysis and progression.

    Strategy:
    1. Find all gap topics (uncovered or stale).
    2. For each recently covered topic, check its adjacent topics.
       If an adjacent topic is in the gap list, it gets priority
       (natural progression, e.g. Runtime → Gateway).
    3. Among candidates, weight by engagement scores if available.
    4. Fall back to the first gap topic if no adjacency match.

    Returns None only if every topic is covered and fresh.
    """
    gaps = identify_topic_gaps(coverage_map)
    if not gaps:
        return None

    gap_set = set(gaps)

    # Step 1: find adjacent-to-recently-covered topics that are gaps
    adjacent_candidates: list[str] = []
    for entry in _recently_covered_topics(coverage_map):
        for adj in entry.adjacent_topics:
            if adj in gap_set and adj not in adjacent_candidates:
                adjacent_candidates.append(adj)

    # Step 2: pick from adjacent candidates, weighted by engagement if available
    candidates = adjacent_candidates if adjacent_candidates else gaps

    if engagement_scores:
        # Higher engagement score = more audience interest in that topic area
        scored = [(topic, engagement_scores.get(topic, 0.0)) for topic in candidates]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[0][0]

    # No engagement data — return first candidate (natural progression or first gap)
    return candidates[0]


# ---------------------------------------------------------------------------
# Update map after content generation (REQ-033.2, REQ-033.6)
# ---------------------------------------------------------------------------


def update_coverage_map(
    coverage_map: TopicCoverageMap,
    topics_covered: list[str],
    article_titles: list[str],
    run_date: date,
) -> list[dict]:
    """Update the map with topics covered in the current run.

    - New topics: added as covered entries with the run date.
    - Existing topics: preserved with updated dates and appended titles.
    - Returns a list of log entries for the Agent_Log.
    """
    log_entries: list[dict] = []

    for topic_name in topics_covered:
        entry = _find_entry(coverage_map, topic_name)

        if entry is not None:
            # Existing topic — update coverage
            was_covered = entry.covered
            entry.covered = True
            entry.last_covered_date = run_date
            # Append new titles, avoiding duplicates
            for title in article_titles:
                if title not in entry.article_titles:
                    entry.article_titles.append(title)

            log_entries.append({
                "action": "updated" if was_covered else "newly_covered",
                "topic": topic_name,
                "run_date": str(run_date),
                "article_titles": article_titles,
            })
            logger.info(
                "Topic coverage updated: %s (was_covered=%s, date=%s)",
                topic_name,
                was_covered,
                run_date,
            )
        else:
            # New topic not in the original niche list — add it
            new_entry = TopicCoverageEntry(
                topic=topic_name,
                covered=True,
                article_titles=list(article_titles),
                last_covered_date=run_date,
                adjacent_topics=ADJACENT_TOPICS.get(topic_name, []),
            )
            coverage_map.entries.append(new_entry)
            log_entries.append({
                "action": "added",
                "topic": topic_name,
                "run_date": str(run_date),
                "article_titles": article_titles,
            })
            logger.info("New topic added to coverage map: %s", topic_name)

    coverage_map.last_updated = run_date
    return log_entries


# ---------------------------------------------------------------------------
# Persist updated map (REQ-033.1, REQ-033.6)
# ---------------------------------------------------------------------------


def save_coverage_map(
    coverage_map: TopicCoverageMap,
    memory: TopicCoverageMemoryProtocol,
) -> None:
    """Save the updated coverage map to long-term memory."""
    memory.save_topic_coverage_map(coverage_map)
    logger.info(
        "TopicCoverageMap saved — %d entries, last_updated=%s",
        len(coverage_map.entries),
        coverage_map.last_updated,
    )
