"""Metadata extraction pipeline for scored articles.

Sends each article to Claude Haiku to extract structured metadata
(title, publication_date, author, publisher, canonical_url) and applies
fallback defaults when fields are missing.

Requirements: REQ-006.1, REQ-006.2, REQ-006.3, REQ-027.2
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

from magic_content_engine.errors import ErrorCollector, StepError
from magic_content_engine.models import Article, ArticleMetadata
from magic_content_engine.model_router import TaskType, get_model

logger = logging.getLogger(__name__)

_DEFAULT_AUTHOR = "Amazon Web Services"
_DEFAULT_PUBLISHER = "Amazon Web Services"

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT_TEMPLATE = """\
You are a metadata extractor. Given the following article information, \
extract structured metadata from the page content.

Article URL: {url}
Article title (discovered): {title}
Article source: {source}

Extract the following fields:
- title: The article title from og:title or HTML <title> element
- publication_date: The publication date from og:published_time or \
article:published_time (format: YYYY-MM-DD if available, otherwise as found)
- author: The author name from author meta tag or visible byline
- publisher: The publisher or site name from og:site_name
- canonical_url: The canonical URL from rel="canonical" or og:url

Respond with ONLY a JSON object (no markdown, no extra text):
{{
  "title": "<string or null>",
  "publication_date": "<string or null>",
  "author": "<string or null>",
  "publisher": "<string or null>",
  "canonical_url": "<string or null>"
}}
"""


# ---------------------------------------------------------------------------
# LLM protocol — allows test doubles (same pattern as scoring.py)
# ---------------------------------------------------------------------------


class LLMExtractor(Protocol):
    """Protocol for the LLM call used during metadata extraction.

    Production implementations call Bedrock via the Strands SDK.
    Tests can supply a simple callable that returns a JSON string.
    """

    def __call__(self, prompt: str, model_id: str) -> str: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_extraction_response(raw: str) -> dict[str, str | None]:
    """Parse the LLM JSON response into a metadata dict.

    Raises ``ValueError`` when the response cannot be parsed.
    """
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM response is not valid JSON: {raw!r}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object, got {type(data).__name__}")

    return {
        "title": data.get("title"),
        "publication_date": data.get("publication_date"),
        "author": data.get("author"),
        "publisher": data.get("publisher"),
        "canonical_url": data.get("canonical_url"),
    }


# ---------------------------------------------------------------------------
# Fallback application (REQ-006.2, REQ-006.3)
# ---------------------------------------------------------------------------


def _apply_fallbacks(metadata: dict[str, str | None], article: Article) -> ArticleMetadata:
    """Build an ArticleMetadata with fallback defaults applied.

    - title falls back to the discovered article title
    - author falls back to "Amazon Web Services"
    - publisher falls back to "Amazon Web Services"
    """
    title = metadata.get("title")
    if not title or not title.strip():
        title = article.title

    author = metadata.get("author")
    if not author or not author.strip():
        author = _DEFAULT_AUTHOR

    publisher = metadata.get("publisher")
    if not publisher or not publisher.strip():
        publisher = _DEFAULT_PUBLISHER

    return ArticleMetadata(
        article_url=article.url,
        title=title.strip(),
        publication_date=metadata.get("publication_date") or None,
        author=author.strip(),
        publisher=publisher.strip(),
        canonical_url=metadata.get("canonical_url") or None,
    )


# ---------------------------------------------------------------------------
# Single-article extraction
# ---------------------------------------------------------------------------


def _extract_article_metadata(
    article: Article,
    llm: LLMExtractor,
    model_id: str,
) -> ArticleMetadata:
    """Extract metadata for a single article via the LLM.

    Returns an ArticleMetadata with fallbacks applied.
    Raises on LLM or parsing failure.
    """
    prompt = EXTRACTION_PROMPT_TEMPLATE.format(
        url=article.url,
        title=article.title,
        source=article.source,
    )
    raw_response = llm(prompt, model_id)
    parsed = _parse_extraction_response(raw_response)
    return _apply_fallbacks(parsed, article)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_metadata(
    articles: list[Article],
    llm: LLMExtractor,
    collector: ErrorCollector | None = None,
) -> list[ArticleMetadata]:
    """Extract metadata for a list of scored articles.

    On per-article extraction failure the error is recorded in *collector*
    and the article is skipped (log-and-continue pattern, REQ-027.2).

    Parameters
    ----------
    articles:
        Scored articles to extract metadata from.
    llm:
        Callable matching the :class:`LLMExtractor` protocol.
    collector:
        Optional error collector. A fresh one is created when *None*.
    """
    if collector is None:
        collector = ErrorCollector()

    model_id = get_model(TaskType.METADATA_EXTRACTION)
    results: list[ArticleMetadata] = []

    for article in articles:
        try:
            metadata = _extract_article_metadata(article, llm, model_id)
            results.append(metadata)
            logger.info("Metadata extracted for: %s", article.url)
        except Exception as exc:
            collector.add(
                StepError(
                    step="extract",
                    target=article.url,
                    error_message=str(exc),
                    context={"model": model_id},
                )
            )
            # Skip this article and continue (REQ-027.2)

    return results
