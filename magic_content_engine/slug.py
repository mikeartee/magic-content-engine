"""Slug generation for output directory naming.

Derives a kebab-case slug from the primary topic of the confirmed
article list and combines it with the run date to produce the output
directory name.

Requirements: REQ-028.1, REQ-028.2, REQ-028.3
"""

from __future__ import annotations

import re
from datetime import date

from magic_content_engine.models import Article

_SLUG_REGEX = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def generate_slug(topic: str) -> str:
    """Convert a topic string into a valid kebab-case slug.

    Rules:
    - Lowercase alphanumeric characters and hyphens only
    - No leading, trailing, or consecutive hyphens
    - Empty result falls back to ``"content"``
    - Output always matches ``^[a-z0-9]+(-[a-z0-9]+)*$``
    """
    slug = topic.lower()
    # Replace any non-alphanumeric character with a hyphen
    slug = re.sub(r"[^a-z0-9]", "-", slug)
    # Collapse consecutive hyphens
    slug = re.sub(r"-{2,}", "-", slug)
    # Strip leading/trailing hyphens
    slug = slug.strip("-")

    if not slug:
        return "content"

    return slug


def derive_topic(articles: list[Article]) -> str:
    """Extract the primary topic from the confirmed article list.

    Uses the title of the highest-scored article. When no articles
    are provided, returns ``"weekly-update"`` as a sensible default.
    """
    if not articles:
        return "weekly-update"

    best = max(articles, key=lambda a: a.relevance_score or 0)
    return best.title


def make_output_dirname(run_date: date, slug: str) -> str:
    """Return the output directory name in ``YYYY-MM-DD-[slug]`` format."""
    return f"{run_date.isoformat()}-{slug}"
