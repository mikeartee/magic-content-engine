# Implementation Plan: S3 Key Alignment

## Overview

Documentation and contract test feature. No runtime code changes. Three deliverables: a Hypothesis-powered contract test file, a README section, and a `.env.example` annotation. All tasks use Python and pytest/Hypothesis.

## Tasks

- [x] 1. Create contract test file with inline Importer logic and static assertions
  - [x] 1.1 Create `magic_content_engine/test_s3_key_contract.py` with imports, inline Importer parsing functions, and static unit tests
    - Create the file at `magic_content_engine/test_s3_key_contract.py`
    - Import `generate_slug`, `make_output_dirname` from `magic_content_engine.slug`
    - Import `S3_KEY_PREFIX` from `magic_content_engine.config`
    - Import `_OUTPUT_FILENAMES` from `magic_content_engine.orchestrator`
    - Implement inline Importer parsing helpers:
      - `importer_parse_date(dir_name: str) -> str` — returns `dir_name[:10]`
      - `importer_parse_slug(dir_name: str) -> str` — returns `dir_name[11:]`
      - `importer_build_key(key_prefix: str) -> str` — returns `f"{key_prefix}/post.md"`
    - Implement `test_known_key_example`: use `date(2025, 7, 14)` and topic `"AgentCore Memory Launch"`, assert full S3 key equals `output/2025-07-14-agentcore-memory-launch/post.md`
    - Implement `test_s3_key_prefix_default`: assert `S3_KEY_PREFIX == "output/"`
    - Implement `test_blog_filename`: assert `_OUTPUT_FILENAMES["blog"] == "post.md"`
    - Implement `test_fallback_slug`: assert `generate_slug("") == "content"`
    - _Requirements: 2.1, 3.3, 5.1, 5.2, 5.3, 5.5, 5.6_

  - [x]* 1.2 Write property test for dir-name round-trip
    - **Property 1: Dir-name round-trip**
    - Use `hypothesis.strategies.dates(min_value=date(2020,1,1), max_value=date(2099,12,31))` and `hypothesis.strategies.from_regex(r'^[a-z0-9]+(-[a-z0-9]+)*$', fullmatch=True)` with `max_size` constraint
    - Assert `importer_parse_date(make_output_dirname(d, s)) == d.isoformat()`
    - Assert `importer_parse_slug(make_output_dirname(d, s)) == s`
    - Tag with comment: `# Feature: s3-key-alignment, Property 1: Dir-name round-trip`
    - **Validates: Requirements 1.3, 1.5, 4.1, 4.2, 4.3, 4.4**

  - [x]* 1.3 Write property test for slug generation validity
    - **Property 2: Slug generation always produces valid slugs**
    - Use `hypothesis.strategies.text()` for arbitrary topic input
    - Assert result matches `^[a-z0-9]+(-[a-z0-9]+)*$`
    - Tag with comment: `# Feature: s3-key-alignment, Property 2: Slug generation always produces valid slugs`
    - **Validates: Requirements 3.1, 3.3**

  - [x]* 1.4 Write property test for end-to-end round-trip through slug generation
    - **Property 3: End-to-end round-trip through slug generation**
    - Use `hypothesis.strategies.text()` and `hypothesis.strategies.dates(min_value=date(2020,1,1), max_value=date(2099,12,31))`
    - Pipeline: `generate_slug(topic)` → `make_output_dirname(d, slug)` → Importer slicing
    - Assert recovered date equals `d.isoformat()` and recovered slug equals `generate_slug(topic)`
    - Tag with comment: `# Feature: s3-key-alignment, Property 3: End-to-end round-trip through slug generation`
    - **Validates: Requirements 1.1, 1.3, 3.1, 3.2, 3.4, 5.4**

- [x] 2. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Add contract documentation
  - [x] 3.1 Add "S3 Key Format" section to `README.md`
    - Insert after the "Output bundle" section
    - Document the full key pattern: `output/{YYYY-MM-DD}-{slug}/post.md`
    - Document slug character set: `^[a-z0-9]+(-[a-z0-9]+)*$` and fallback value `content`
    - Document that `S3_KEY_PREFIX` must equal `output/` for Importer compatibility
    - Document the Importer's fixed-offset parsing: `slice(0, 10)` for date, `slice(11)` for slug
    - Note this is only correct when the date segment is exactly 10 characters and the separator is a single `-`
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [x] 3.2 Add compatibility annotation to `.env.example`
    - Add a comment above `S3_KEY_PREFIX=output/` explaining the Importer hardcodes `Prefix: 'output/'` and changing this value will break discovery
    - _Requirements: 2.3_

- [x] 4. Final checkpoint — Ensure all tests pass
  - Run `pytest magic_content_engine/ -v` and confirm the full suite is green including the new contract tests.
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- No runtime code changes — this is purely documentation and verification
- The contract test re-implements the Importer's TypeScript slicing logic in Python; the Importer itself is not modified
- Hypothesis and pytest are already in `pyproject.toml` dev dependencies
