"""Tests for APA citation builder.

Requirements: REQ-007.1, REQ-007.2, REQ-007.3, REQ-007.4
"""

from __future__ import annotations

import pytest

from magic_content_engine.errors import ErrorCollector
from magic_content_engine.models import ArticleMetadata, APACitation
from magic_content_engine.citation import (
    _extract_year,
    _extract_surname,
    _make_bibtex_key,
    build_in_text_citation,
    build_bibtex_entry,
    build_citation,
    build_citations,
    aggregate_bibtex,
    format_apa_reference,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_metadata(
    author: str = "Jane Doe",
    title: str = "AgentCore Launch",
    publication_date: str | None = "2025-07-14",
    publisher: str = "AWS Blog",
    url: str = "https://aws.amazon.com/blogs/agentcore",
    canonical_url: str | None = None,
) -> ArticleMetadata:
    return ArticleMetadata(
        article_url=url,
        title=title,
        publication_date=publication_date,
        author=author,
        publisher=publisher,
        canonical_url=canonical_url,
    )


def _make_llm(response: str | Exception):
    """Return a fake LLM callable."""
    def fake_llm(prompt: str, model_id: str) -> str:
        if isinstance(response, Exception):
            raise response
        return response
    return fake_llm


# ---------------------------------------------------------------------------
# _extract_year
# ---------------------------------------------------------------------------


class TestExtractYear:
    def test_iso_date(self):
        assert _extract_year("2025-07-14") == "2025"

    def test_year_only(self):
        assert _extract_year("2024") == "2024"

    def test_none_returns_nd(self):
        assert _extract_year(None) == "n.d."

    def test_empty_returns_nd(self):
        assert _extract_year("") == "n.d."

    def test_no_year_returns_nd(self):
        assert _extract_year("no date here") == "n.d."


# ---------------------------------------------------------------------------
# _extract_surname
# ---------------------------------------------------------------------------


class TestExtractSurname:
    def test_personal_name_two_words(self):
        assert _extract_surname("Jane Doe") == "Doe"

    def test_organisational_name(self):
        assert _extract_surname("Amazon Web Services") == "Amazon Web Services"

    def test_comma_separated(self):
        assert _extract_surname("Doe, J.") == "Doe"

    def test_single_word(self):
        assert _extract_surname("AWS") == "AWS"


# ---------------------------------------------------------------------------
# build_in_text_citation
# ---------------------------------------------------------------------------


class TestBuildInTextCitation:
    def test_personal_author(self):
        m = _make_metadata(author="Jane Doe", publication_date="2025-07-14")
        assert build_in_text_citation(m) == "(Doe, 2025)"

    def test_organisational_author(self):
        m = _make_metadata(author="Amazon Web Services", publication_date="2025-01-01")
        assert build_in_text_citation(m) == "(Amazon Web Services, 2025)"

    def test_no_date(self):
        m = _make_metadata(author="Jane Doe", publication_date=None)
        assert build_in_text_citation(m) == "(Doe, n.d.)"


# ---------------------------------------------------------------------------
# _make_bibtex_key
# ---------------------------------------------------------------------------


class TestMakeBibtexKey:
    def test_personal_author(self):
        m = _make_metadata(author="Jane Doe", publication_date="2025-07-14")
        assert _make_bibtex_key(m) == "doe_2025"

    def test_organisational_author(self):
        m = _make_metadata(author="Amazon Web Services", publication_date="2025-01-01")
        assert _make_bibtex_key(m) == "amazon_web_services_2025"

    def test_no_date(self):
        m = _make_metadata(author="Jane Doe", publication_date=None)
        assert _make_bibtex_key(m) == "doe_n_d"


# ---------------------------------------------------------------------------
# build_bibtex_entry
# ---------------------------------------------------------------------------


class TestBuildBibtexEntry:
    def test_complete_entry(self):
        m = _make_metadata(
            author="Jane Doe",
            title="AgentCore Launch",
            publication_date="2025-07-14",
            publisher="AWS Blog",
            url="https://aws.amazon.com/blogs/agentcore",
        )
        entry = build_bibtex_entry(m)
        assert entry.startswith("@online{doe_2025,")
        assert "author    = {Jane Doe}" in entry
        assert "title     = {AgentCore Launch}" in entry
        assert "year      = {2025}" in entry
        assert "date      = {2025-07-14}" in entry
        assert "url       = {https://aws.amazon.com/blogs/agentcore}" in entry
        assert "publisher = {AWS Blog}" in entry
        assert entry.endswith("}")

    def test_uses_canonical_url_when_present(self):
        m = _make_metadata(
            url="https://example.com/post",
            canonical_url="https://example.com/canonical",
        )
        entry = build_bibtex_entry(m)
        assert "url       = {https://example.com/canonical}" in entry

    def test_no_date_uses_nd(self):
        m = _make_metadata(publication_date=None)
        entry = build_bibtex_entry(m)
        assert "year      = {n.d.}" in entry
        assert "date      = {n.d.}" in entry


# ---------------------------------------------------------------------------
# format_apa_reference (LLM call)
# ---------------------------------------------------------------------------


class TestFormatApaReference:
    def test_returns_stripped_llm_output(self):
        m = _make_metadata()
        llm = _make_llm("  Doe, J. (2025, July 14). *AgentCore Launch*. AWS Blog. https://aws.amazon.com/blogs/agentcore  ")
        result = format_apa_reference(m, llm, "test-model")
        assert result == "Doe, J. (2025, July 14). *AgentCore Launch*. AWS Blog. https://aws.amazon.com/blogs/agentcore"

    def test_llm_failure_propagates(self):
        m = _make_metadata()
        llm = _make_llm(RuntimeError("LLM down"))
        with pytest.raises(RuntimeError, match="LLM down"):
            format_apa_reference(m, llm, "test-model")


# ---------------------------------------------------------------------------
# build_citation (single article)
# ---------------------------------------------------------------------------


class TestBuildCitation:
    def test_builds_complete_citation(self):
        m = _make_metadata(author="Jane Doe", publication_date="2025-07-14")
        llm = _make_llm("Doe, J. (2025, July 14). *AgentCore Launch*. AWS Blog. https://aws.amazon.com/blogs/agentcore")
        citation = build_citation(m, llm, "test-model")

        assert isinstance(citation, APACitation)
        assert citation.metadata is m
        assert "Doe" in citation.reference_entry
        assert citation.in_text_citation == "(Doe, 2025)"
        assert citation.bibtex_entry.startswith("@online{doe_2025,")


# ---------------------------------------------------------------------------
# build_citations (batch with error handling)
# ---------------------------------------------------------------------------


class TestBuildCitations:
    def test_successful_batch(self):
        llm = _make_llm("Doe, J. (2025). *Title*. Site. https://example.com")
        metadata_list = [_make_metadata(), _make_metadata(author="Bob Smith")]
        results = build_citations(metadata_list, llm)
        assert len(results) == 2

    def test_failure_skips_and_continues(self):
        call_count = 0

        def mixed_llm(prompt: str, model_id: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Transient failure")
            return "Smith, B. (2025). *Title*. Site. https://example.com"

        collector = ErrorCollector()
        metadata_list = [
            _make_metadata(url="https://fail.example.com"),
            _make_metadata(url="https://good.example.com", author="Bob Smith"),
        ]
        results = build_citations(metadata_list, mixed_llm, collector=collector)

        assert len(results) == 1
        assert results[0].metadata.article_url == "https://good.example.com"
        assert collector.has_errors
        assert collector.errors[0].step == "cite"
        assert collector.errors[0].target == "https://fail.example.com"

    def test_empty_list(self):
        results = build_citations([], _make_llm(""))
        assert results == []


# ---------------------------------------------------------------------------
# aggregate_bibtex
# ---------------------------------------------------------------------------


class TestAggregateBibtex:
    def test_sorted_alphabetically(self):
        m_b = _make_metadata(author="Bob Smith", publication_date="2025-01-01")
        m_a = _make_metadata(author="Alice Jones", publication_date="2024-06-01")
        llm = _make_llm("ref entry")

        c_b = build_citation(m_b, llm, "test-model")
        c_a = build_citation(m_a, llm, "test-model")

        bib = aggregate_bibtex([c_b, c_a])
        # Alice (a) should come before Bob (b) when sorted
        idx_a = bib.index("@online{jones_2024")
        idx_b = bib.index("@online{smith_2025")
        assert idx_a < idx_b

    def test_contains_all_entries(self):
        llm = _make_llm("ref entry")
        citations = [
            build_citation(_make_metadata(author="Author One", publication_date="2025-01-01"), llm, "m"),
            build_citation(_make_metadata(author="Author Two", publication_date="2025-02-01"), llm, "m"),
            build_citation(_make_metadata(author="Author Three", publication_date="2025-03-01"), llm, "m"),
        ]
        bib = aggregate_bibtex(citations)
        assert bib.count("@online{") == 3

    def test_empty_list_returns_empty(self):
        assert aggregate_bibtex([]) == ""

    def test_single_entry(self):
        llm = _make_llm("ref entry")
        c = build_citation(_make_metadata(), llm, "m")
        bib = aggregate_bibtex([c])
        assert bib.count("@online{") == 1
        assert bib.endswith("\n")
