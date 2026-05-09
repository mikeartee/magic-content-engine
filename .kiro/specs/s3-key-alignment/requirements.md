# Requirements Document

## Introduction

The magic-content-engine uploads blog post files to S3 under a structured key pattern.
The mikefromnz admin importer reads from S3 using the same pattern to list and import
posts as draft content. This spec defines the S3 key format contract between the two
systems, verifies the current implementations are aligned, identifies any gaps, and
requires a contract test to validate the format end-to-end.

The key format is: `output/{YYYY-MM-DD}-{slug}/post.md`

Both systems currently implement this pattern, but the contract has never been formally
documented or tested. This spec closes that gap.

## Glossary

- **Engine**: The magic-content-engine Python application that generates and uploads content.
- **Importer**: The mikefromnz admin page TypeScript component that lists and downloads engine output from S3.
- **S3_Key**: The full object path within an S3 bucket, e.g. `output/2025-07-14-my-post/post.md`.
- **Key_Prefix**: The directory portion of an S3 key without the trailing slash, e.g. `output/2025-07-14-my-post`.
- **Dir_Name**: The `{YYYY-MM-DD}-{slug}` segment of the key, produced by `make_output_dirname()` in the Engine.
- **Slug**: A kebab-case string derived from the article topic, matching `^[a-z0-9]+(-[a-z0-9]+)*$`.
- **S3_KEY_PREFIX**: An environment variable on the Engine that sets the root prefix for all uploads. Defaults to `output/`.
- **Contract_Test**: A test that validates the S3 key format produced by the Engine can be correctly parsed by the Importer.
- **Round_Trip**: The property that a value serialised by one system can be deserialised by the other to recover the original value.

## Requirements

### Requirement 1: S3 Key Format Contract

**User Story:** As a developer, I want a formally documented S3 key format contract between the Engine and the Importer, so that both systems can be maintained independently without breaking the integration.

#### Acceptance Criteria

1. THE Engine SHALL produce S3 keys matching the pattern `output/{YYYY-MM-DD}-{slug}/post.md` where `{YYYY-MM-DD}` is the ISO 8601 run date and `{slug}` matches `^[a-z0-9]+(-[a-z0-9]+)*$`.
2. THE Importer SHALL list engine outputs by querying S3 with `Prefix: 'output/'` and `Delimiter: '/'` and parsing the returned `CommonPrefixes`.
3. WHEN the Importer receives a `Key_Prefix` of the form `output/{YYYY-MM-DD}-{slug}`, THE Importer SHALL extract the date as `keyPrefix.split('/')[1].slice(0, 10)` and the slug as `keyPrefix.split('/')[1].slice(11)`.
4. WHEN the Importer downloads a post, THE Importer SHALL request the S3 key `{Key_Prefix}/post.md`.
5. THE Contract_Test SHALL verify that a `Dir_Name` produced by `make_output_dirname(run_date, slug)` in the Engine can be parsed by the Importer's date and slug extraction logic to recover the original `run_date` and `slug`.

### Requirement 2: S3_KEY_PREFIX Configuration

**User Story:** As an operator, I want the Engine's S3_KEY_PREFIX to be explicitly set to `output/` in all deployment environments, so that the Importer's hardcoded `output/` prefix assumption is always satisfied.

#### Acceptance Criteria

1. THE Engine SHALL read the S3 key prefix from the `S3_KEY_PREFIX` environment variable with a default value of `output/`.
2. WHEN `S3_KEY_PREFIX` is set to any value other than `output/`, THE Engine SHALL produce S3 keys that the Importer cannot discover, because the Importer uses a hardcoded `Prefix: 'output/'`.
3. THE Engine's `.env.example` file SHALL document that `S3_KEY_PREFIX` must be set to `output/` for compatibility with the Importer.
4. IF `S3_KEY_PREFIX` does not end with a `/`, THEN THE Engine SHALL still produce valid S3 keys by ensuring the Dir_Name is separated from the prefix by exactly one `/`.

### Requirement 3: Slug Character Set Compatibility

**User Story:** As a developer, I want to confirm that slugs produced by the Engine are always valid inputs to the Importer's parsing logic, so that no slug causes a silent parse failure.

#### Acceptance Criteria

1. THE Engine's `generate_slug()` function SHALL produce slugs matching `^[a-z0-9]+(-[a-z0-9]+)*$`.
2. THE Importer's slug extraction SHALL correctly handle any slug matching `^[a-z0-9]+(-[a-z0-9]+)*$`, because this character set is a strict subset of the Importer's accepted path characters.
3. WHEN `generate_slug()` receives a topic that produces an empty string after normalisation, THE Engine SHALL substitute the fallback slug `content`.
4. THE Contract_Test SHALL verify that the Engine's slug character set is a subset of the characters the Importer can parse without error.

### Requirement 4: Date Segment Parsing Alignment

**User Story:** As a developer, I want to verify that the Importer's fixed-offset date extraction matches the Engine's Dir_Name format exactly, so that date parsing never silently produces a wrong value.

#### Acceptance Criteria

1. THE Engine SHALL format the date segment of Dir_Name using `date.isoformat()`, which always produces exactly 10 characters in `YYYY-MM-DD` format.
2. THE Importer SHALL extract the date from a Dir_Name by taking `parts.slice(0, 10)`, which is correct if and only if the date segment is always exactly 10 characters.
3. THE Importer SHALL extract the slug from a Dir_Name by taking `parts.slice(11)`, which is correct if and only if the separator between date and slug is always exactly one `-` character at position 10.
4. THE Contract_Test SHALL assert that for any valid `run_date` and `slug`, `make_output_dirname(run_date, slug).slice(0, 10)` equals `run_date.isoformat()` and `make_output_dirname(run_date, slug).slice(11)` equals `slug`.

### Requirement 5: Contract Test

**User Story:** As a developer, I want an automated contract test that validates the S3 key format end-to-end, so that any future change to either system that breaks the integration is caught immediately.

#### Acceptance Criteria

1. THE Contract_Test SHALL be located in `magic-content-engine/magic_content_engine/test_s3_key_contract.py`.
2. THE Contract_Test SHALL import `make_output_dirname` and `generate_slug` from the Engine and apply the Importer's parsing logic inline to verify Round_Trip correctness.
3. WHEN given a known `run_date` and `topic`, THE Contract_Test SHALL verify that the full S3 key equals `output/{make_output_dirname(run_date, slug)}/post.md`.
4. THE Contract_Test SHALL verify the Round_Trip property: for a set of representative topics and dates, applying `generate_slug(topic)`, then `make_output_dirname(run_date, slug)`, then the Importer's `slice(0, 10)` and `slice(11)` extractions recovers the original date string and slug.
5. THE Contract_Test SHALL verify that the `S3_KEY_PREFIX` default value in `config.py` equals `output/`.
6. THE Contract_Test SHALL verify that `_OUTPUT_FILENAMES["blog"]` in `orchestrator.py` equals `post.md`.
7. IF any of the above assertions fail, THEN THE Contract_Test SHALL produce a descriptive failure message identifying which part of the contract is broken.

### Requirement 6: Contract Documentation

**User Story:** As a developer, I want the agreed S3 key format documented in a single authoritative location, so that future contributors understand the integration contract without reading both codebases.

#### Acceptance Criteria

1. THE Engine's `README.md` SHALL include a section titled "S3 Key Format" that documents the full key pattern `output/{YYYY-MM-DD}-{slug}/post.md`.
2. THE "S3 Key Format" section SHALL document the slug character set `^[a-z0-9]+(-[a-z0-9]+)*$` and the fallback value `content`.
3. THE "S3 Key Format" section SHALL document that `S3_KEY_PREFIX` must equal `output/` for compatibility with the Importer.
4. THE "S3 Key Format" section SHALL note that the Importer uses fixed-offset parsing (`slice(0, 10)` for date, `slice(11)` for slug) and that this is only correct when the date segment is exactly 10 characters and the separator is a single `-`.
