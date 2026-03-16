"""APA 7th edition citation builder.

Pipeline stages (metadata extraction and fallback application are handled
upstream in metadata.py):
  1. APA reference formatting via Claude Haiku
  2. In-text citation generation (programmatic)
  3. BibTeX entry generation (programmatic)
  4. Aggregation into references.bib (sorted alphabetically)

Requirements: REQ-007.1, REQ-007.2, REQ-007.3, REQ-007.4
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from magic_content_engine.errors import ErrorCollector, StepError
from magic_content_engine.models import APACitation, ArticleMetadata
from magic_content_engine.model_router import TaskType, get_model

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM protocol — same pattern as metadata.py / scoring.py
# ---------------------------------------------------------------------------


class LLMFormatter(Protocol):
    """Protocol for the LLM call used during APA formatting.

    Production implementations call Bedrock via the Strands SDK.
    Tests can supply a simple callable that returns a string.
    """

    def __call__(self, prompt: str, model_id: str) -> str: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# APA formatting prompt (sent to Haiku)
# ---------------------------------------------------------------------------

APA_FORMAT_PROMPT_TEMPLATE = """\
You are an APA 7th edition citation formatter. Given the following article \
metadata, produce a single APA reference entry.

Author: {author}
Publication date: {publication_date}
Title: {title}
Site name: {publisher}
URL: {url}

Format the reference entry exactly as:
Author, A. A. (Year, Month Day). *Title*. Site Name. URL

Rules:
- If the author is an organisation (e.g. "Amazon Web Services"), use the \
full name without abbreviation.
- If the publication date is missing or "n.d.", use (n.d.) for the date.
- The title should be in sentence case and italicised with asterisks.
- The URL should be the full URL with no trailing period.

Respond with ONLY the formatted reference entry (no extra text, no quotes).
"""


# ---------------------------------------------------------------------------
# APA reference formatting (LLM)
# ---------------------------------------------------------------------------


def _build_apa_prompt(metadata: ArticleMetadata) -> str:
    """Build the APA formatting prompt from metadata."""
    return APA_FORMAT_PROMPT_TEMPLATE.format(
        author=metadata.author,
        publication_date=metadata.publication_date or "n.d.",
        title=metadata.title,
        publisher=metadata.publisher,
        url=metadata.canonical_url or metadata.article_url,
    )


def format_apa_reference(
    metadata: ArticleMetadata,
    llm: LLMFormatter,
    model_id: str,
) -> str:
    """Format an APA 7th edition reference entry via the LLM.

    Returns the formatted reference string.
    Raises on LLM failure.
    """
    prompt = _build_apa_prompt(metadata)
    raw = llm(prompt, model_id)
    return raw.strip()


# ---------------------------------------------------------------------------
# In-text citation (programmatic — no LLM needed)
# ---------------------------------------------------------------------------


def _extract_year(publication_date: str | None) -> str:
    """Extract a four-digit year from a date string, or return 'n.d.'."""
    if not publication_date:
        return "n.d."
    match = re.search(r"\b(\d{4})\b", publication_date)
    return match.group(1) if match else "n.d."


def _extract_surname(author: str) -> str:
    """Extract the surname for in-text citation.

    For organisational authors (e.g. "Amazon Web Services"), return the
    full name.  For personal names, return the last word (surname).
    """
    # Heuristic: if the author contains 3+ words and no comma,
    # treat as organisational name
    words = author.strip().split()
    if len(words) >= 3 and "," not in author:
        return author.strip()
    # For "Surname, F. F." or "First Last" patterns
    if "," in author:
        return author.split(",")[0].strip()
    return words[-1] if words else author.strip()


def build_in_text_citation(metadata: ArticleMetadata) -> str:
    """Build an in-text citation in the format (Surname, Year).

    Returns e.g. ``(Doe, 2025)`` or ``(Amazon Web Services, 2025)``.
    """
    surname = _extract_surname(metadata.author)
    year = _extract_year(metadata.publication_date)
    return f"({surname}, {year})"


# ---------------------------------------------------------------------------
# BibTeX generation (programmatic — no LLM needed)
# ---------------------------------------------------------------------------


def _make_bibtex_key(metadata: ArticleMetadata) -> str:
    """Generate a BibTeX citation key from author surname and year.

    Format: ``surname_year`` in lowercase with non-alphanumeric chars
    replaced by underscores.
    """
    surname = _extract_surname(metadata.author).lower()
    year = _extract_year(metadata.publication_date)
    key = re.sub(r"[^a-z0-9]", "_", f"{surname}_{year}")
    # Collapse consecutive underscores
    key = re.sub(r"_+", "_", key).strip("_")
    return key


def build_bibtex_entry(metadata: ArticleMetadata) -> str:
    """Build a ``@online{}`` BibTeX entry from article metadata."""
    key = _make_bibtex_key(metadata)
    url = metadata.canonical_url or metadata.article_url
    year = _extract_year(metadata.publication_date)
    date_field = metadata.publication_date or "n.d."

    lines = [
        f"@online{{{key},",
        f"  author    = {{{metadata.author}}},",
        f"  title     = {{{metadata.title}}},",
        f"  year      = {{{year}}},",
        f"  date      = {{{date_field}}},",
        f"  url       = {{{url}}},",
        f"  publisher = {{{metadata.publisher}}},",
        "}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Single-article citation builder
# ---------------------------------------------------------------------------


def build_citation(
    metadata: ArticleMetadata,
    llm: LLMFormatter,
    model_id: str,
) -> APACitation:
    """Build a complete APA citation for a single article.

    Calls the LLM for the reference entry, then programmatically
    generates the in-text citation and BibTeX entry.
    """
    reference_entry = format_apa_reference(metadata, llm, model_id)
    in_text = build_in_text_citation(metadata)
    bibtex = build_bibtex_entry(metadata)

    return APACitation(
        metadata=metadata,
        reference_entry=reference_entry,
        in_text_citation=in_text,
        bibtex_entry=bibtex,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_citations(
    metadata_list: list[ArticleMetadata],
    llm: LLMFormatter,
    collector: ErrorCollector | None = None,
) -> list[APACitation]:
    """Build APA citations for a list of article metadata records.

    On per-article failure the error is recorded in *collector* and the
    article is skipped (log-and-continue pattern, REQ-027.2).
    """
    if collector is None:
        collector = ErrorCollector()

    model_id = get_model(TaskType.APA_CITATION)
    results: list[APACitation] = []

    for metadata in metadata_list:
        try:
            citation = build_citation(metadata, llm, model_id)
            results.append(citation)
            logger.info("Citation built for: %s", metadata.article_url)
        except Exception as exc:
            collector.add(
                StepError(
                    step="cite",
                    target=metadata.article_url,
                    error_message=str(exc),
                    context={"model": model_id},
                )
            )

    return results


def aggregate_bibtex(citations: list[APACitation]) -> str:
    """Aggregate all BibTeX entries into a single string, sorted alphabetically.

    The output is suitable for writing directly to ``references.bib``.
    """
    entries = sorted(
        (c.bibtex_entry for c in citations),
        key=lambda entry: entry.lower(),
    )
    return "\n\n".join(entries) + "\n" if entries else ""
