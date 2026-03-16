"""Unit tests for topic coverage map persistence and gap analysis."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from magic_content_engine.models import TopicCoverageEntry, TopicCoverageMap
from magic_content_engine.topic_coverage import (
    ADJACENT_TOPICS,
    NICHE_TOPICS,
    TopicCoverageMemoryProtocol,
    create_empty_coverage_map,
    derive_recommended_focus,
    identify_topic_gaps,
    load_or_create_coverage_map,
    save_coverage_map,
    update_coverage_map,
)


# ---------------------------------------------------------------------------
# Lightweight memory stub
# ---------------------------------------------------------------------------


class StubTopicMemory:
    """In-memory stub implementing TopicCoverageMemoryProtocol."""

    def __init__(self, stored_map: Optional[TopicCoverageMap] = None) -> None:
        self._stored: Optional[TopicCoverageMap] = stored_map
        self.save_count: int = 0

    def load_topic_coverage_map(self) -> Optional[TopicCoverageMap]:
        return self._stored

    def save_topic_coverage_map(self, coverage_map: TopicCoverageMap) -> None:
        self._stored = coverage_map
        self.save_count += 1


def _make_entry(
    topic: str,
    covered: bool = False,
    titles: Optional[list[str]] = None,
    last_date: Optional[date] = None,
    adjacent: Optional[list[str]] = None,
) -> TopicCoverageEntry:
    return TopicCoverageEntry(
        topic=topic,
        covered=covered,
        article_titles=titles or [],
        last_covered_date=last_date,
        adjacent_topics=adjacent or [],
    )


# ---------------------------------------------------------------------------
# Tests — empty map / first run (REQ-033.1)
# ---------------------------------------------------------------------------


class TestEmptyMapFirstRun:
    def test_create_empty_map_has_all_niche_topics(self) -> None:
        m = create_empty_coverage_map(date(2025, 7, 14))
        topic_names = [e.topic for e in m.entries]
        assert topic_names == NICHE_TOPICS

    def test_create_empty_map_all_uncovered(self) -> None:
        m = create_empty_coverage_map(date(2025, 7, 14))
        assert all(not e.covered for e in m.entries)
        assert all(e.last_covered_date is None for e in m.entries)

    def test_create_empty_map_sets_last_updated(self) -> None:
        run = date(2025, 7, 14)
        m = create_empty_coverage_map(run)
        assert m.last_updated == run

    def test_create_empty_map_populates_adjacent_topics(self) -> None:
        m = create_empty_coverage_map(date(2025, 7, 14))
        runtime = next(e for e in m.entries if e.topic == "AgentCore Runtime")
        assert runtime.adjacent_topics == ["AgentCore Gateway"]

    def test_load_returns_empty_map_when_memory_empty(self) -> None:
        memory = StubTopicMemory(stored_map=None)
        m = load_or_create_coverage_map(memory, date(2025, 7, 14))
        assert len(m.entries) == len(NICHE_TOPICS)
        assert all(not e.covered for e in m.entries)

    def test_load_returns_existing_map_when_present(self) -> None:
        existing = TopicCoverageMap(
            entries=[_make_entry("Kiro IDE", covered=True)],
            last_updated=date(2025, 7, 7),
        )
        memory = StubTopicMemory(stored_map=existing)
        m = load_or_create_coverage_map(memory, date(2025, 7, 14))
        assert len(m.entries) == 1
        assert m.entries[0].topic == "Kiro IDE"

    def test_stub_satisfies_protocol(self) -> None:
        assert isinstance(StubTopicMemory(), TopicCoverageMemoryProtocol)


# ---------------------------------------------------------------------------
# Tests — gap identification (REQ-033.3)
# ---------------------------------------------------------------------------


class TestGapIdentification:
    def test_all_uncovered_returns_all_topics(self) -> None:
        m = create_empty_coverage_map(date(2025, 7, 14))
        gaps = identify_topic_gaps(m)
        assert set(gaps) == set(NICHE_TOPICS)

    def test_partially_covered_returns_uncovered_only(self) -> None:
        m = create_empty_coverage_map(date(2025, 7, 14))
        # Cover two topics
        for e in m.entries:
            if e.topic in ("Kiro IDE", "Bedrock"):
                e.covered = True
                e.last_covered_date = date.today()
        gaps = identify_topic_gaps(m)
        assert "Kiro IDE" not in gaps
        assert "Bedrock" not in gaps
        assert "AgentCore Runtime" in gaps

    def test_stale_topics_included_in_gaps(self) -> None:
        m = TopicCoverageMap(
            entries=[
                _make_entry("Kiro IDE", covered=True, last_date=date.today() - timedelta(days=60)),
                _make_entry("Bedrock", covered=True, last_date=date.today()),
            ],
            last_updated=date.today(),
        )
        gaps = identify_topic_gaps(m, staleness_days=30)
        assert "Kiro IDE" in gaps
        assert "Bedrock" not in gaps

    def test_no_gaps_when_all_fresh(self) -> None:
        today = date.today()
        m = TopicCoverageMap(
            entries=[_make_entry(t, covered=True, last_date=today) for t in NICHE_TOPICS],
            last_updated=today,
        )
        gaps = identify_topic_gaps(m)
        assert gaps == []


# ---------------------------------------------------------------------------
# Tests — recommended focus derivation (REQ-033.4)
# ---------------------------------------------------------------------------


class TestRecommendedFocus:
    def test_adjacent_topic_preferred(self) -> None:
        """Runtime covered → Gateway recommended (natural progression)."""
        today = date.today()
        m = TopicCoverageMap(
            entries=[
                _make_entry("AgentCore Runtime", covered=True, last_date=today, adjacent=["AgentCore Gateway"]),
                _make_entry("AgentCore Gateway", covered=False),
                _make_entry("Kiro IDE", covered=False),
            ],
            last_updated=today,
        )
        focus = derive_recommended_focus(m)
        assert focus == "AgentCore Gateway"

    def test_falls_back_to_first_gap_when_no_adjacency(self) -> None:
        m = TopicCoverageMap(
            entries=[
                _make_entry("Kiro IDE", covered=False),
                _make_entry("Bedrock", covered=False),
            ],
            last_updated=date.today(),
        )
        focus = derive_recommended_focus(m)
        assert focus in ("Kiro IDE", "Bedrock")

    def test_returns_none_when_all_covered_and_fresh(self) -> None:
        today = date.today()
        m = TopicCoverageMap(
            entries=[_make_entry(t, covered=True, last_date=today) for t in NICHE_TOPICS],
            last_updated=today,
        )
        focus = derive_recommended_focus(m)
        assert focus is None

    def test_engagement_scores_weight_selection(self) -> None:
        m = TopicCoverageMap(
            entries=[
                _make_entry("Kiro IDE", covered=False),
                _make_entry("Bedrock", covered=False),
                _make_entry("MCP Protocol", covered=False),
            ],
            last_updated=date.today(),
        )
        scores = {"Kiro IDE": 1.0, "Bedrock": 5.0, "MCP Protocol": 2.0}
        focus = derive_recommended_focus(m, engagement_scores=scores)
        assert focus == "Bedrock"

    def test_empty_map_first_run_returns_first_niche_topic(self) -> None:
        m = create_empty_coverage_map(date(2025, 7, 14))
        focus = derive_recommended_focus(m)
        # Should return some topic from the gap list
        assert focus is not None
        assert focus in NICHE_TOPICS


# ---------------------------------------------------------------------------
# Tests — update coverage map (REQ-033.2, REQ-033.6)
# ---------------------------------------------------------------------------


class TestUpdateCoverageMap:
    def test_update_existing_uncovered_topic(self) -> None:
        m = create_empty_coverage_map(date(2025, 7, 7))
        run = date(2025, 7, 14)
        logs = update_coverage_map(m, ["Kiro IDE"], ["New Kiro Feature"], run)

        entry = next(e for e in m.entries if e.topic == "Kiro IDE")
        assert entry.covered is True
        assert entry.last_covered_date == run
        assert "New Kiro Feature" in entry.article_titles
        assert len(logs) == 1
        assert logs[0]["action"] == "newly_covered"

    def test_update_already_covered_topic_preserves_old_titles(self) -> None:
        m = TopicCoverageMap(
            entries=[
                _make_entry("Kiro IDE", covered=True, titles=["Old Article"], last_date=date(2025, 7, 7)),
            ],
            last_updated=date(2025, 7, 7),
        )
        run = date(2025, 7, 14)
        logs = update_coverage_map(m, ["Kiro IDE"], ["New Article"], run)

        entry = m.entries[0]
        assert entry.last_covered_date == run
        assert "Old Article" in entry.article_titles
        assert "New Article" in entry.article_titles
        assert logs[0]["action"] == "updated"

    def test_update_adds_unknown_topic(self) -> None:
        m = create_empty_coverage_map(date(2025, 7, 7))
        original_count = len(m.entries)
        run = date(2025, 7, 14)
        logs = update_coverage_map(m, ["Brand New Topic"], ["Article X"], run)

        assert len(m.entries) == original_count + 1
        new_entry = next(e for e in m.entries if e.topic == "Brand New Topic")
        assert new_entry.covered is True
        assert logs[0]["action"] == "added"

    def test_update_sets_last_updated(self) -> None:
        m = create_empty_coverage_map(date(2025, 7, 7))
        run = date(2025, 7, 14)
        update_coverage_map(m, ["Bedrock"], ["Article"], run)
        assert m.last_updated == run

    def test_update_no_duplicate_titles(self) -> None:
        m = TopicCoverageMap(
            entries=[_make_entry("Kiro IDE", covered=True, titles=["Same Title"])],
            last_updated=date(2025, 7, 7),
        )
        update_coverage_map(m, ["Kiro IDE"], ["Same Title"], date(2025, 7, 14))
        entry = m.entries[0]
        assert entry.article_titles.count("Same Title") == 1

    def test_returns_log_entries_for_agent_log(self) -> None:
        m = create_empty_coverage_map(date(2025, 7, 7))
        logs = update_coverage_map(
            m, ["Kiro IDE", "Bedrock"], ["Article A", "Article B"], date(2025, 7, 14)
        )
        assert len(logs) == 2
        assert all("topic" in log and "run_date" in log for log in logs)


# ---------------------------------------------------------------------------
# Tests — save coverage map
# ---------------------------------------------------------------------------


class TestSaveCoverageMap:
    def test_save_calls_memory(self) -> None:
        memory = StubTopicMemory()
        m = create_empty_coverage_map(date(2025, 7, 14))
        save_coverage_map(m, memory)
        assert memory.save_count == 1

    def test_saved_map_is_retrievable(self) -> None:
        memory = StubTopicMemory()
        m = create_empty_coverage_map(date(2025, 7, 14))
        save_coverage_map(m, memory)
        loaded = memory.load_topic_coverage_map()
        assert loaded is m


# ---------------------------------------------------------------------------
# Tests — adjacent topic tracking
# ---------------------------------------------------------------------------


class TestAdjacentTopicTracking:
    def test_agentcore_chain_complete(self) -> None:
        """Verify the full AgentCore progression chain is defined."""
        chain = ["AgentCore Runtime", "AgentCore Gateway", "AgentCore Browser",
                 "AgentCore Memory", "AgentCore Identity", "AgentCore Observability"]
        for i, topic in enumerate(chain[:-1]):
            assert chain[i + 1] in ADJACENT_TOPICS[topic]

    def test_empty_map_entries_have_correct_adjacency(self) -> None:
        m = create_empty_coverage_map(date(2025, 7, 14))
        runtime = next(e for e in m.entries if e.topic == "AgentCore Runtime")
        assert runtime.adjacent_topics == ["AgentCore Gateway"]
        kiro = next(e for e in m.entries if e.topic == "Kiro IDE")
        assert kiro.adjacent_topics == []
