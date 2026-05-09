"""Contract tests: S3 key format alignment between Engine and Importer.

Validates that the Engine's S3 key production is compatible with the
Importer's fixed-offset parsing logic. The Importer's TypeScript slicing
is replicated inline in Python — no cross-language dependency needed.

Requirements: 2.1, 3.3, 5.1, 5.2, 5.3, 5.5, 5.6
"""

from __future__ import annotations

from datetime import date

from magic_content_engine.config import S3_KEY_PREFIX
from magic_content_engine.orchestrator import _OUTPUT_FILENAMES
from magic_content_engine.slug import _SLUG_REGEX, generate_slug, make_output_dirname


# ---------------------------------------------------------------------------
# Inline Importer parsing helpers (mirrors s3Ops.ts logic)
# ---------------------------------------------------------------------------


def importer_parse_date(dir_name: str) -> str:
    """Replicates: parts.slice(0, 10) from s3Ops.ts"""
    return dir_name[:10]


def importer_parse_slug(dir_name: str) -> str:
    """Replicates: parts.slice(11) from s3Ops.ts"""
    return dir_name[11:]


def importer_build_key(key_prefix: str) -> str:
    """Replicates: `${keyPrefix}/post.md` from s3Ops.ts"""
    return f"{key_prefix}/post.md"


# ---------------------------------------------------------------------------
# Static unit tests
# ---------------------------------------------------------------------------


def test_known_key_example() -> None:
    """A concrete date + topic produces the expected full S3 key."""
    run_date = date(2025, 7, 14)
    topic = "AgentCore Memory Launch"
    slug = generate_slug(topic)
    dir_name = make_output_dirname(run_date, slug)
    full_key = f"{S3_KEY_PREFIX}{dir_name}/post.md"

    assert full_key == "output/2025-07-14-agentcore-memory-launch/post.md"


def test_s3_key_prefix_default() -> None:
    """S3_KEY_PREFIX must default to 'output/' for Importer compatibility."""
    assert S3_KEY_PREFIX == "output/"


def test_blog_filename() -> None:
    """The blog output filename must be 'post.md'."""
    assert _OUTPUT_FILENAMES["blog"] == "post.md"


def test_fallback_slug() -> None:
    """An empty topic must produce the fallback slug 'content'."""
    assert generate_slug("") == "content"


# ---------------------------------------------------------------------------
# Property-based tests (Hypothesis)
# ---------------------------------------------------------------------------

from hypothesis import given
from hypothesis import strategies as st


# Feature: s3-key-alignment, Property 1: Dir-name round-trip
# Validates: Requirements 1.3, 1.5, 4.1, 4.2, 4.3, 4.4
@given(
    d=st.dates(min_value=date(2020, 1, 1), max_value=date(2099, 12, 31)),
    s=st.from_regex(r"^[a-z0-9]+(-[a-z0-9]+)*$", fullmatch=True).filter(
        lambda s: len(s) <= 50
    ),
)
def test_dirname_roundtrip(d: date, s: str) -> None:
    """Dir-name round-trip: Importer parsing recovers original date and slug."""
    dir_name = make_output_dirname(d, s)
    assert importer_parse_date(dir_name) == d.isoformat()
    assert importer_parse_slug(dir_name) == s


# Feature: s3-key-alignment, Property 2: Slug generation always produces valid slugs
# Validates: Requirements 3.1, 3.3
@given(topic=st.text())
def test_slug_always_valid(topic: str) -> None:
    """Slug generation always produces a string matching the slug regex."""
    slug = generate_slug(topic)
    assert _SLUG_REGEX.match(slug), f"Invalid slug {slug!r} from topic {topic!r}"


# Feature: s3-key-alignment, Property 3: End-to-end round-trip through slug generation
# Validates: Requirements 1.1, 1.3, 3.1, 3.2, 3.4, 5.4
@given(
    topic=st.text(),
    d=st.dates(min_value=date(2020, 1, 1), max_value=date(2099, 12, 31)),
)
def test_end_to_end_roundtrip(topic: str, d: date) -> None:
    """End-to-end round-trip: generate_slug → make_output_dirname → Importer slicing."""
    slug = generate_slug(topic)
    dir_name = make_output_dirname(d, slug)
    assert importer_parse_date(dir_name) == d.isoformat()
    assert importer_parse_slug(dir_name) == generate_slug(topic)
