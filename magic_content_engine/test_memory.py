"""Tests for AgentCore Memory session and long-term usage.

Requirements: REQ-022.1, REQ-022.2, REQ-022.3
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from magic_content_engine.memory import (
    AgentCoreLongTermMemory,
    LocalLongTermMemory,
    LongTermMemoryProtocol,
    SessionMemory,
    get_long_term_memory,
)
from magic_content_engine.models import (
    Article,
    HeldItem,
    PostEngagement,
    TopicCoverageEntry,
    TopicCoverageMap,
)


# ---------------------------------------------------------------------------
# SessionMemory (short-term) — REQ-022.1
# ---------------------------------------------------------------------------


class TestSessionMemory:
    def test_initial_state_is_empty(self):
        mem = SessionMemory()
        assert mem.get_articles() == []
        assert mem.get_selected_outputs() == []
        assert mem.scored_article_urls == set()

    def test_set_and_get_articles(self):
        mem = SessionMemory()
        articles = [
            Article(url="https://a.com", title="A", source="s", source_type="primary", discovered_date=date(2025, 1, 1)),
        ]
        mem.set_articles(articles)
        result = mem.get_articles()
        assert len(result) == 1
        assert result[0].url == "https://a.com"

    def test_get_articles_returns_copy(self):
        mem = SessionMemory()
        mem.set_articles([
            Article(url="https://a.com", title="A", source="s", source_type="primary", discovered_date=date(2025, 1, 1)),
        ])
        copy = mem.get_articles()
        copy.clear()
        assert len(mem.get_articles()) == 1

    def test_mark_scored_and_is_scored(self):
        mem = SessionMemory()
        assert not mem.is_scored("https://a.com")
        mem.mark_scored("https://a.com")
        assert mem.is_scored("https://a.com")
        assert not mem.is_scored("https://b.com")

    def test_set_and_get_selected_outputs(self):
        mem = SessionMemory()
        mem.set_selected_outputs(["blog", "youtube"])
        assert mem.get_selected_outputs() == ["blog", "youtube"]

    def test_get_selected_outputs_returns_copy(self):
        mem = SessionMemory()
        mem.set_selected_outputs(["blog"])
        copy = mem.get_selected_outputs()
        copy.append("cfp")
        assert mem.get_selected_outputs() == ["blog"]

    def test_clear_resets_all_state(self):
        mem = SessionMemory()
        mem.set_articles([
            Article(url="https://a.com", title="A", source="s", source_type="primary", discovered_date=date(2025, 1, 1)),
        ])
        mem.mark_scored("https://a.com")
        mem.set_selected_outputs(["blog"])
        mem.clear()
        assert mem.get_articles() == []
        assert not mem.is_scored("https://a.com")
        assert mem.get_selected_outputs() == []


# ---------------------------------------------------------------------------
# LocalLongTermMemory — REQ-022.2
# ---------------------------------------------------------------------------


class TestLocalLongTermMemory:
    @pytest.fixture()
    def mem(self, tmp_path: Path) -> LocalLongTermMemory:
        return LocalLongTermMemory(tmp_path)

    # --- covered URLs ---

    def test_covered_urls_empty_on_first_load(self, mem: LocalLongTermMemory):
        assert mem.load_covered_urls() == {}

    def test_store_and_load_covered_urls(self, mem: LocalLongTermMemory):
        urls = {"https://a.com": "2025-01-01", "https://b.com": "2025-01-08"}
        mem.store_covered_urls(urls)
        assert mem.load_covered_urls() == urls

    # --- voice profile ---

    def test_voice_profile_empty_on_first_load(self, mem: LocalLongTermMemory):
        assert mem.load_voice_profile() == ""

    def test_store_and_load_voice_profile(self, mem: LocalLongTermMemory):
        mem.store_voice_profile("Conversational, first-person, honest")
        assert mem.load_voice_profile() == "Conversational, first-person, honest"

    # --- content preferences ---

    def test_content_preferences_empty_on_first_load(self, mem: LocalLongTermMemory):
        assert mem.load_content_preferences() == {}

    def test_store_and_load_content_preferences(self, mem: LocalLongTermMemory):
        prefs = {"default_outputs": ["blog", "digest"], "niche_focus": "AgentCore"}
        mem.store_content_preferences(prefs)
        assert mem.load_content_preferences() == prefs

    # --- TopicCoverageMap ---

    def test_topic_coverage_none_on_first_load(self, mem: LocalLongTermMemory):
        assert mem.load_topic_coverage_map() is None

    def test_store_and_load_topic_coverage_map(self, mem: LocalLongTermMemory):
        tcm = TopicCoverageMap(
            entries=[
                TopicCoverageEntry(
                    topic="AgentCore Runtime",
                    covered=True,
                    article_titles=["Runtime launch"],
                    last_covered_date=date(2025, 7, 1),
                    adjacent_topics=["AgentCore Gateway"],
                ),
                TopicCoverageEntry(
                    topic="Kiro IDE",
                    covered=False,
                    article_titles=[],
                ),
            ],
            last_updated=date(2025, 7, 1),
            recommended_focus="Kiro IDE",
        )
        mem.save_topic_coverage_map(tcm)
        loaded = mem.load_topic_coverage_map()
        assert loaded is not None
        assert len(loaded.entries) == 2
        assert loaded.entries[0].topic == "AgentCore Runtime"
        assert loaded.entries[0].covered is True
        assert loaded.entries[0].last_covered_date == date(2025, 7, 1)
        assert loaded.entries[1].topic == "Kiro IDE"
        assert loaded.entries[1].covered is False
        assert loaded.last_updated == date(2025, 7, 1)
        assert loaded.recommended_focus == "Kiro IDE"

    # --- Engagement_Metrics ---

    def test_engagements_empty_on_first_load(self, mem: LocalLongTermMemory):
        assert mem.load_engagements() == []

    def test_store_and_load_engagements(self, mem: LocalLongTermMemory):
        eng = PostEngagement(
            post_title="My Post",
            publication_date=date(2025, 6, 1),
            url="https://dev.to/mike/my-post",
            views=100,
            reactions=10,
            comments=3,
            reading_time_minutes=5,
            last_fetched=date(2025, 7, 1),
        )
        mem.save_engagements([eng])
        loaded = mem.load_engagements()
        assert len(loaded) == 1
        assert loaded[0].post_title == "My Post"
        assert loaded[0].views == 100
        assert loaded[0].last_fetched == date(2025, 7, 1)

    # --- HeldItems ---

    def test_held_items_empty_on_first_load(self, mem: LocalLongTermMemory):
        assert mem.load_held_items() == []

    def test_save_and_load_held_item(self, mem: LocalLongTermMemory):
        item = HeldItem(
            filename="post.md",
            s3_destination_path="output/2025-07-14-launch/post.md",
            release_date=date(2025, 7, 21),
            article_titles=["Launch article"],
            run_date=date(2025, 7, 14),
            local_file_path="./output/held/2025-07-14-launch/post.md",
        )
        mem.save_held_item(item)
        loaded = mem.load_held_items()
        assert len(loaded) == 1
        assert loaded[0].filename == "post.md"
        assert loaded[0].release_date == date(2025, 7, 21)

    def test_remove_held_item(self, mem: LocalLongTermMemory):
        item1 = HeldItem(
            filename="post.md",
            s3_destination_path="output/a/post.md",
            release_date=date(2025, 7, 21),
            article_titles=["A"],
            run_date=date(2025, 7, 14),
            local_file_path="./output/held/a/post.md",
        )
        item2 = HeldItem(
            filename="script.md",
            s3_destination_path="output/a/script.md",
            release_date=date(2025, 7, 28),
            article_titles=["B"],
            run_date=date(2025, 7, 14),
            local_file_path="./output/held/a/script.md",
        )
        mem.save_held_item(item1)
        mem.save_held_item(item2)
        assert len(mem.load_held_items()) == 2

        mem.remove_held_item(item1)
        remaining = mem.load_held_items()
        assert len(remaining) == 1
        assert remaining[0].filename == "script.md"


# ---------------------------------------------------------------------------
# AgentCoreLongTermMemory — production stub
# ---------------------------------------------------------------------------


class TestAgentCoreLongTermMemory:
    def test_raises_not_implemented_for_all_methods(self):
        mem = AgentCoreLongTermMemory()
        with pytest.raises(NotImplementedError, match="not yet wired"):
            mem.load_covered_urls()
        with pytest.raises(NotImplementedError):
            mem.store_covered_urls({})
        with pytest.raises(NotImplementedError):
            mem.load_voice_profile()
        with pytest.raises(NotImplementedError):
            mem.store_voice_profile("")
        with pytest.raises(NotImplementedError):
            mem.load_content_preferences()
        with pytest.raises(NotImplementedError):
            mem.store_content_preferences({})
        with pytest.raises(NotImplementedError):
            mem.load_topic_coverage_map()
        with pytest.raises(NotImplementedError):
            mem.load_engagements()
        with pytest.raises(NotImplementedError):
            mem.load_held_items()

    def test_satisfies_protocol(self):
        assert isinstance(AgentCoreLongTermMemory(), LongTermMemoryProtocol)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_local_satisfies_protocol(self, tmp_path: Path):
        assert isinstance(LocalLongTermMemory(tmp_path), LongTermMemoryProtocol)


# ---------------------------------------------------------------------------
# Factory — get_long_term_memory
# ---------------------------------------------------------------------------


class TestGetLongTermMemory:
    def test_returns_local_by_default(self, tmp_path: Path):
        mem = get_long_term_memory(local_dir=tmp_path)
        assert isinstance(mem, LocalLongTermMemory)

    def test_returns_agentcore_when_requested(self):
        mem = get_long_term_memory(use_agentcore=True)
        assert isinstance(mem, AgentCoreLongTermMemory)


# ---------------------------------------------------------------------------
# REQ-022.3: voice profile loaded at run start
# ---------------------------------------------------------------------------


class TestVoiceProfileLoadAtRunStart:
    """Verify that a stored voice profile can be loaded at run start."""

    def test_load_voice_profile_returns_stored_value(self, tmp_path: Path):
        mem = LocalLongTermMemory(tmp_path)
        profile = "Conversational, first-person, honest about gaps"
        mem.store_voice_profile(profile)
        assert mem.load_voice_profile() == profile

    def test_load_content_preferences_returns_stored_value(self, tmp_path: Path):
        mem = LocalLongTermMemory(tmp_path)
        prefs = {"niche_focus": "AI Engineering on AWS", "default_outputs": ["blog"]}
        mem.store_content_preferences(prefs)
        loaded = mem.load_content_preferences()
        assert loaded["niche_focus"] == "AI Engineering on AWS"
