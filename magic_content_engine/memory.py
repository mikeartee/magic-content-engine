"""AgentCore Memory integration for session and long-term state.

Provides:
- SessionMemory: in-process short-term memory for a single run
  (current article list, scoring progress, selected outputs).
- LongTermMemoryProtocol: interface for persistent cross-run state
  (covered URLs, run dates, voice profile, content preferences,
  TopicCoverageMap, Engagement_Metrics, HeldItems).
- LocalLongTermMemory: JSON-file-backed implementation for local dev.
- AgentCoreLongTermMemory: production stub for AgentCore Memory service.
- get_long_term_memory(): factory returning the appropriate backend.

Requirements: REQ-022.1, REQ-022.2, REQ-022.3
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from magic_content_engine.models import (
    Article,
    HeldItem,
    PostEngagement,
    TopicCoverageEntry,
    TopicCoverageMap,
)

logger = logging.getLogger(__name__)

_DEFAULT_LOCAL_DIR = Path(".memory")


# ---------------------------------------------------------------------------
# Short-term (session) memory — REQ-022.1
# ---------------------------------------------------------------------------


@dataclass
class SessionMemory:
    """In-process session state that lives for the duration of a single run.

    Tracks the current article list, scoring progress, and selected
    outputs. Discarded when the run ends.
    """

    articles: list[Article] = field(default_factory=list)
    scored_article_urls: set[str] = field(default_factory=set)
    selected_outputs: list[str] = field(default_factory=list)

    # --- articles ---

    def set_articles(self, articles: list[Article]) -> None:
        """Store the current article list."""
        self.articles = list(articles)

    def get_articles(self) -> list[Article]:
        """Return the current article list."""
        return list(self.articles)

    # --- scoring progress ---

    def mark_scored(self, url: str) -> None:
        """Record that an article URL has been scored."""
        self.scored_article_urls.add(url)

    def is_scored(self, url: str) -> bool:
        """Check whether an article URL has already been scored."""
        return url in self.scored_article_urls

    # --- selected outputs ---

    def set_selected_outputs(self, outputs: list[str]) -> None:
        """Store the user's selected output types."""
        self.selected_outputs = list(outputs)

    def get_selected_outputs(self) -> list[str]:
        """Return the selected output types."""
        return list(self.selected_outputs)

    def clear(self) -> None:
        """Reset all session state."""
        self.articles.clear()
        self.scored_article_urls.clear()
        self.selected_outputs.clear()


# ---------------------------------------------------------------------------
# Long-term memory protocol — REQ-022.2
# ---------------------------------------------------------------------------


@runtime_checkable
class LongTermMemoryProtocol(Protocol):
    """Interface for persistent cross-run memory.

    In production this is backed by AgentCore Memory (long-term).
    Tests and local development use LocalLongTermMemory (JSON files).
    """

    # --- covered URLs ---

    def load_covered_urls(self) -> dict[str, str]:
        """Load previously covered article URLs.

        Returns a mapping of URL -> run date (ISO string).
        """
        ...

    def store_covered_urls(self, urls: dict[str, str]) -> None:
        """Persist covered article URLs with their run dates."""
        ...

    # --- voice profile ---

    def load_voice_profile(self) -> str:
        """Load the content owner's voice profile text."""
        ...

    def store_voice_profile(self, profile: str) -> None:
        """Persist the voice profile text."""
        ...

    # --- content preferences ---

    def load_content_preferences(self) -> dict[str, Any]:
        """Load content preferences (output defaults, niche focus, etc.)."""
        ...

    def store_content_preferences(self, prefs: dict[str, Any]) -> None:
        """Persist content preferences."""
        ...

    # --- TopicCoverageMap ---

    def load_topic_coverage_map(self) -> Optional[TopicCoverageMap]:
        """Load the persisted TopicCoverageMap, or None if not stored."""
        ...

    def save_topic_coverage_map(self, coverage_map: TopicCoverageMap) -> None:
        """Persist the TopicCoverageMap."""
        ...

    # --- Engagement_Metrics ---

    def load_engagements(self) -> list[PostEngagement]:
        """Load all stored PostEngagement records."""
        ...

    def save_engagements(self, engagements: list[PostEngagement]) -> None:
        """Persist PostEngagement records."""
        ...

    # --- HeldItems ---

    def load_held_items(self) -> list[HeldItem]:
        """Load all HeldItems from memory."""
        ...

    def save_held_item(self, item: HeldItem) -> None:
        """Persist a single HeldItem."""
        ...

    def remove_held_item(self, item: HeldItem) -> None:
        """Remove a single HeldItem from memory."""
        ...


# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------


def _serialize_topic_coverage_map(m: TopicCoverageMap) -> dict:
    return {
        "entries": [
            {
                "topic": e.topic,
                "covered": e.covered,
                "article_titles": e.article_titles,
                "last_covered_date": e.last_covered_date.isoformat() if e.last_covered_date else None,
                "adjacent_topics": e.adjacent_topics,
            }
            for e in m.entries
        ],
        "last_updated": m.last_updated.isoformat(),
        "recommended_focus": m.recommended_focus,
    }


def _deserialize_topic_coverage_map(data: dict) -> TopicCoverageMap:
    entries = []
    for e in data.get("entries", []):
        lcd = e.get("last_covered_date")
        entries.append(
            TopicCoverageEntry(
                topic=e["topic"],
                covered=e["covered"],
                article_titles=e.get("article_titles", []),
                last_covered_date=date.fromisoformat(lcd) if lcd else None,
                adjacent_topics=e.get("adjacent_topics", []),
            )
        )
    return TopicCoverageMap(
        entries=entries,
        last_updated=date.fromisoformat(data["last_updated"]),
        recommended_focus=data.get("recommended_focus"),
    )


def _serialize_engagement(e: PostEngagement) -> dict:
    return {
        "post_title": e.post_title,
        "publication_date": e.publication_date.isoformat(),
        "url": e.url,
        "views": e.views,
        "reactions": e.reactions,
        "comments": e.comments,
        "reading_time_minutes": e.reading_time_minutes,
        "last_fetched": e.last_fetched.isoformat() if e.last_fetched else None,
    }


def _deserialize_engagement(data: dict) -> PostEngagement:
    lf = data.get("last_fetched")
    return PostEngagement(
        post_title=data["post_title"],
        publication_date=date.fromisoformat(data["publication_date"]),
        url=data["url"],
        views=data.get("views", 0),
        reactions=data.get("reactions", 0),
        comments=data.get("comments", 0),
        reading_time_minutes=data.get("reading_time_minutes", 0),
        last_fetched=date.fromisoformat(lf) if lf else None,
    )


def _serialize_held_item(item: HeldItem) -> dict:
    return {
        "filename": item.filename,
        "s3_destination_path": item.s3_destination_path,
        "release_date": item.release_date.isoformat(),
        "article_titles": item.article_titles,
        "run_date": item.run_date.isoformat(),
        "local_file_path": item.local_file_path,
    }


def _deserialize_held_item(data: dict) -> HeldItem:
    return HeldItem(
        filename=data["filename"],
        s3_destination_path=data["s3_destination_path"],
        release_date=date.fromisoformat(data["release_date"]),
        article_titles=data.get("article_titles", []),
        run_date=date.fromisoformat(data["run_date"]),
        local_file_path=data["local_file_path"],
    )


# ---------------------------------------------------------------------------
# LocalLongTermMemory — JSON file backend for local development
# ---------------------------------------------------------------------------


class LocalLongTermMemory:
    """JSON-file-backed long-term memory for local development.

    Stores each data category in a separate JSON file under a
    configurable directory (default: ``.memory/``).
    """

    def __init__(self, base_dir: Path | str = _DEFAULT_LOCAL_DIR) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def _read_json(self, filename: str) -> Any:
        path = self._base / filename
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, filename: str, data: Any) -> None:
        path = self._base / filename
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    # --- covered URLs ---

    def load_covered_urls(self) -> dict[str, str]:
        data = self._read_json("covered_urls.json")
        return data if isinstance(data, dict) else {}

    def store_covered_urls(self, urls: dict[str, str]) -> None:
        self._write_json("covered_urls.json", urls)

    # --- voice profile ---

    def load_voice_profile(self) -> str:
        data = self._read_json("voice_profile.json")
        if isinstance(data, dict):
            return data.get("profile", "")
        return ""

    def store_voice_profile(self, profile: str) -> None:
        self._write_json("voice_profile.json", {"profile": profile})

    # --- content preferences ---

    def load_content_preferences(self) -> dict[str, Any]:
        data = self._read_json("content_preferences.json")
        return data if isinstance(data, dict) else {}

    def store_content_preferences(self, prefs: dict[str, Any]) -> None:
        self._write_json("content_preferences.json", prefs)

    # --- TopicCoverageMap ---

    def load_topic_coverage_map(self) -> Optional[TopicCoverageMap]:
        data = self._read_json("topic_coverage.json")
        if data is None:
            return None
        return _deserialize_topic_coverage_map(data)

    def save_topic_coverage_map(self, coverage_map: TopicCoverageMap) -> None:
        self._write_json("topic_coverage.json", _serialize_topic_coverage_map(coverage_map))

    # --- Engagement_Metrics ---

    def load_engagements(self) -> list[PostEngagement]:
        data = self._read_json("engagements.json")
        if not isinstance(data, list):
            return []
        return [_deserialize_engagement(d) for d in data]

    def save_engagements(self, engagements: list[PostEngagement]) -> None:
        self._write_json("engagements.json", [_serialize_engagement(e) for e in engagements])

    # --- HeldItems ---

    def load_held_items(self) -> list[HeldItem]:
        data = self._read_json("held_items.json")
        if not isinstance(data, list):
            return []
        return [_deserialize_held_item(d) for d in data]

    def save_held_item(self, item: HeldItem) -> None:
        items = self.load_held_items()
        items.append(item)
        self._write_json("held_items.json", [_serialize_held_item(i) for i in items])

    def remove_held_item(self, item: HeldItem) -> None:
        items = self.load_held_items()
        items = [i for i in items if i.filename != item.filename or i.run_date != item.run_date]
        self._write_json("held_items.json", [_serialize_held_item(i) for i in items])


# ---------------------------------------------------------------------------
# AgentCoreLongTermMemory — production stub
# ---------------------------------------------------------------------------


class AgentCoreLongTermMemory:
    """Production provider backed by AgentCore Memory service.

    This is a stub. Production wiring will replace the implementation
    once AgentCore Memory SDK integration is complete.
    """

    def _not_implemented(self) -> None:
        raise NotImplementedError(
            "AgentCoreLongTermMemory is not yet wired to AgentCore Memory. "
            "Production memory integration will be implemented during deployment."
        )

    def load_covered_urls(self) -> dict[str, str]:
        self._not_implemented()
        return {}  # unreachable, keeps type checker happy

    def store_covered_urls(self, urls: dict[str, str]) -> None:
        self._not_implemented()

    def load_voice_profile(self) -> str:
        self._not_implemented()
        return ""

    def store_voice_profile(self, profile: str) -> None:
        self._not_implemented()

    def load_content_preferences(self) -> dict[str, Any]:
        self._not_implemented()
        return {}

    def store_content_preferences(self, prefs: dict[str, Any]) -> None:
        self._not_implemented()

    def load_topic_coverage_map(self) -> Optional[TopicCoverageMap]:
        self._not_implemented()
        return None

    def save_topic_coverage_map(self, coverage_map: TopicCoverageMap) -> None:
        self._not_implemented()

    def load_engagements(self) -> list[PostEngagement]:
        self._not_implemented()
        return []

    def save_engagements(self, engagements: list[PostEngagement]) -> None:
        self._not_implemented()

    def load_held_items(self) -> list[HeldItem]:
        self._not_implemented()
        return []

    def save_held_item(self, item: HeldItem) -> None:
        self._not_implemented()

    def remove_held_item(self, item: HeldItem) -> None:
        self._not_implemented()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_long_term_memory(
    use_agentcore: bool = False,
    local_dir: Path | str = _DEFAULT_LOCAL_DIR,
) -> LongTermMemoryProtocol:
    """Return the appropriate long-term memory backend.

    Args:
        use_agentcore: When True, returns AgentCoreLongTermMemory
            for production use. Defaults to False (local development).
        local_dir: Directory for JSON files in local mode.
    """
    if use_agentcore:
        return AgentCoreLongTermMemory()
    return LocalLongTermMemory(local_dir)
