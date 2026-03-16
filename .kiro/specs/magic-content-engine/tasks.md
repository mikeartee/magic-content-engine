# Implementation Plan: Magic Content Engine

## Overview

Incremental implementation of the Magic Content Engine — a scheduled, AWS-hosted Strands agent application that automates weekly content research and generation. Built with Python (Strands Agents SDK), deployed on AgentCore Runtime, using Claude Haiku for structured tasks and Claude Sonnet for narrative writing. Implementation proceeds in 13 phases: project setup, data models, source crawling, deduplication, relevance scoring, metadata & citations, topic coverage map, engagement signal tracking, weekly brief, user interaction & output selection, content generation, output bundle & publish gate, and final integration.

## Tasks

- [ ] 1. Project structure and configuration
  - [x] 1.1 Create Python package structure and entry point
    - Create `magic_content_engine/` package with `__init__.py`
    - Create `magic_content_engine/orchestrator.py` as a minimal stub with CLI entry point and argument parsing only (`python -m magic_content_engine.orchestrator --source manual`). Do not implement workflow logic here — that belongs in task 17.1
    - Create `magic_content_engine/config.py` with all environment variables from the design (S3_BUCKET, STEERING_BASE_PATH, HAIKU_MODEL_ID, SONNET_MODEL_ID, RELEVANCE_THRESHOLD, SCREENSHOT_VIEWPORT_W/H, SCREENSHOT_WAIT_S, MAX_RETRY_ATTEMPTS, LOG_LEVEL, SES_SENDER_EMAIL, SES_RECIPIENT_EMAIL, HELD_OUTPUT_PATH, REVIEW_OUTPUT_PATH, DEVTO_API_KEY, DEVTO_USERNAME)
    - Create `.env.example` with placeholder values; add `.env` to `.gitignore`
    - Create `pyproject.toml` with dependencies: strands-agents, hypothesis, boto3, python-dotenv
    - _Requirements: REQ-019.1, REQ-021.2, REQ-034.5_

  - [x] 1.2 Implement model router
    - Create `magic_content_engine/model_router.py` with `TaskType` enum and `MODEL_ROUTING` dict
    - Haiku for: RELEVANCE_SCORING, METADATA_EXTRACTION, APA_CITATION, DIGEST_EMAIL, WEEKLY_BRIEF
    - Sonnet for: BLOG_POST, YOUTUBE_SCRIPT, CFP_ABSTRACT, USERGROUP_OUTLINE
    - Implement `get_model(task: TaskType) -> str`
    - _Requirements: REQ-010.1, REQ-010.2, REQ-035.7_

  - [ ]* 1.3 Write property test for model routing (Property 12)
    - **Property 12: Model routing correctness**
    - For all TaskType values, verify correct model returned per design routing table
    - Use `@settings(max_examples=100)` with `st.sampled_from(TaskType)`
    - **Validates: REQ-010.1, REQ-010.2**

  - [x] 1.4 Implement error handling foundation
    - Create `magic_content_engine/errors.py` with `StepError` dataclass
    - Implement log-and-continue error collection pattern
    - Implement S3 retry with exponential backoff (1s, 2s, 4s) and source crawl retry (3 attempts, 2s fixed delay)
    - _Requirements: REQ-027.1, REQ-027.2, REQ-027.3, REQ-027.4, REQ-027.5, REQ-024.2_

  - [ ]* 1.5 Write unit tests for error handling
    - Test S3 exponential backoff timing (1s, 2s, 4s)
    - Test source crawl retry (3 attempts with 2s delay)
    - Test SES failure logged without retry
    - Test log-and-continue collects errors without aborting
    - _Requirements: REQ-027.1–REQ-027.5, REQ-024.2_

- [ ] 2. Data models
  - [x] 2.1 Implement core data models
    - Create `magic_content_engine/models.py` with all dataclasses from design:
      - `Article` (url, title, source, source_type, discovered_date, relevance_score, scoring_rationale, status)
      - `ArticleMetadata` (article_url, title, publication_date, author, publisher, canonical_url) with "Amazon Web Services" defaults
      - `APACitation` (metadata, reference_entry, in_text_citation, bibtex_entry)
      - `OutputBundle` (run_date, slug, selected_outputs, generated_files, references_bib, cost_estimate, agent_log, s3_key_prefix)
      - `CostEstimate` and `ModelInvocation`
      - `AgentLog` (run_date, invocation_source, articles_found, articles_kept, articles, model_usage, screenshot_results, errors, selected_outputs, run_metadata)
      - `ScreenshotCapture` (target_url, filename, viewport_width=1440, viewport_height=900, wait_seconds=3, success, error)
      - `HeldItem` (filename, s3_destination_path, release_date, article_titles, run_date, local_file_path)
      - `ReviewItem` (filename, run_date, local_file_path, reason)
      - `TopicCoverageEntry` (topic, covered, article_titles, last_covered_date, adjacent_topics)
      - `TopicCoverageMap` (entries, last_updated, recommended_focus)
      - `PostEngagement` (post_title, publication_date, url, views, reactions, comments, reading_time_minutes, last_fetched)
      - `WeeklyBrief` (run_date, top_post, coverage_map, recommended_focus, user_override, clean_state)
    - _Requirements: REQ-005.1, REQ-006.1–REQ-006.3, REQ-007.1–REQ-007.3, REQ-016.1, REQ-017.1–REQ-017.4, REQ-026.1–REQ-026.3, REQ-030.5, REQ-033.1, REQ-034.1–REQ-034.2, REQ-035.2_

  - [ ]* 2.2 Write property test for relevance score range (Property 4)
    - **Property 4: Relevance score range invariant**
    - Generate random Article instances, verify relevance_score is int in [1, 5]
    - Use `@settings(max_examples=100)`
    - **Validates: REQ-005.1**

  - [ ]* 2.3 Write property test for metadata completeness with fallbacks (Property 6)
    - **Property 6: Metadata completeness with fallbacks**
    - Generate ArticleMetadata with missing author/publisher fields, verify fallbacks applied ("Amazon Web Services")
    - Use `@settings(max_examples=100)` with `st.none() | st.text()` for optional fields
    - **Validates: REQ-006.1, REQ-006.2, REQ-006.3**

  - [ ]* 2.4 Write property test for slug format (Property 15)
    - **Property 15: Slug format invariant**
    - Generate random topic strings, produce slugs, verify each matches `^[a-z0-9]+(-[a-z0-9]+)*$`
    - Verify output directory name matches `YYYY-MM-DD-[slug]`
    - Use `@settings(max_examples=100)`
    - **Validates: REQ-028.1, REQ-028.2, REQ-028.3**

- [x] 3. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Source crawling
  - [x] 4.1 Implement primary source crawler
    - Create `magic_content_engine/crawler.py`
    - Implement crawling for all 5 primary sources via AgentCore Browser:
      - kiro.dev/changelog/ide/
      - github.com/kirodotdev/Kiro/issues
      - aws.amazon.com/new/ (with keyword filter for "bedrock", "agentcore", "kiro", "lambda")
      - aws.amazon.com/blogs/machine-learning/
      - community.aws/
    - Implement 3-attempt retry with 2s fixed delay per source
    - Log failures in Agent_Log and continue crawling remaining sources
    - _Requirements: REQ-002.1, REQ-002.2, REQ-002.3_

  - [ ]* 4.2 Write property test for AWS news keyword filter (Property 3)
    - **Property 3: AWS news keyword filter correctness**
    - Generate random article text with/without keywords, verify filter passes only when at least one keyword present (case-insensitive)
    - Use `@settings(max_examples=100)` with `st.text()` and `st.sampled_from(["bedrock", "agentcore", "kiro", "lambda"])`
    - **Validates: REQ-002.2**

  - [x] 4.3 Implement secondary source crawler
    - Extend `crawler.py` to crawl all 4 secondary sources:
      - github.com/awslabs/ (new AgentCore and Strands releases)
      - strandsagents.com
      - repost.aws/ (trending AI/ML)
      - kiro.dev/blog/
    - Same retry logic (3 attempts, 2s delay) and error handling as primary
    - _Requirements: REQ-003.1, REQ-003.2_

  - [ ]* 4.4 Write unit tests for source crawling
    - Test all 5 primary sources are crawled
    - Test all 4 secondary sources are crawled
    - Test retry after 3 failures logs error and continues
    - Test keyword filter on aws.amazon.com/new/ articles
    - _Requirements: REQ-002.1–REQ-002.3, REQ-003.1–REQ-003.2_

- [ ] 5. Deduplication and relevance scoring
  - [x] 5.1 Implement article deduplication
    - Create `magic_content_engine/deduplication.py`
    - Query AgentCore Memory (long-term) for each discovered article URL
    - Mark matched articles as "previously_covered" and exclude from scoring
    - Store confirmed article URLs and run date in long-term memory after user confirmation
    - _Requirements: REQ-004.1, REQ-004.2, REQ-004.3_

  - [ ]* 5.2 Write property test for deduplication round-trip (Property 2)
    - **Property 2: Article deduplication round-trip**
    - Store random URLs in mock memory, rediscover them, verify exclusion; verify unknown URLs pass through
    - Use `@settings(max_examples=100)` with `st.text()` for URLs, `st.dates()` for run dates
    - **Validates: REQ-004.1, REQ-004.2, REQ-004.3**

  - [x] 5.3 Implement relevance scoring pipeline
    - Create `magic_content_engine/scoring.py`
    - Send each article to Claude Haiku for relevance scoring (1–5 integer scale)
    - Apply scoring criteria from REQ-005.2 (High 4-5, Medium 3, Low 1-2)
    - Exclude articles scoring below threshold (default 3)
    - Record score and rationale in Agent_Log
    - On scoring failure: log error, skip article, continue remaining
    - _Requirements: REQ-005.1, REQ-005.2, REQ-005.3, REQ-005.4, REQ-027.2_

  - [ ]* 5.4 Write property test for score threshold filter (Property 5)
    - **Property 5: Score threshold filter**
    - Generate random scored article sets, verify only articles with score ≥ 3 pass through
    - Use `@settings(max_examples=100)` with `st.integers(min_value=1, max_value=5)`
    - **Validates: REQ-005.3, REQ-008.1**

- [ ] 6. Metadata extraction and APA citation building
  - [x] 6.1 Implement metadata extraction
    - Create `magic_content_engine/metadata.py`
    - Extract title, publication_date, author, publisher, canonical_url via Claude Haiku
    - Apply fallback "Amazon Web Services" for missing author or publisher
    - On extraction failure: log error, skip article, continue remaining
    - _Requirements: REQ-006.1, REQ-006.2, REQ-006.3, REQ-027.2_

  - [x] 6.2 Implement APA citation builder
    - Create `magic_content_engine/citation.py`
    - Pipeline: metadata extraction → fallback application → APA formatting (Haiku) → in-text citation → BibTeX generation → aggregation
    - APA format: `Author, A. A. (Year, Month Day). *Title*. Site Name. URL`
    - In-text: `(Surname, Year)` or `(Amazon Web Services, Year)`
    - BibTeX: `@online{}` block with all metadata fields
    - Aggregate all BibTeX entries into `references.bib`, sorted alphabetically
    - _Requirements: REQ-007.1, REQ-007.2, REQ-007.3, REQ-007.4_

  - [ ]* 6.3 Write property test for APA citation round-trip (Property 7)
    - **Property 7: APA citation round-trip**
    - Generate random ArticleMetadata, format as APA, parse back, verify equivalence
    - Use `@settings(max_examples=100)` with random `ArticleMetadata` instances
    - **Validates: REQ-007.5**

  - [ ]* 6.4 Write property test for citation components (Property 8)
    - **Property 8: Citation contains all required components**
    - Generate random ArticleMetadata, verify APA ref contains author/year/title/site/URL, in-text matches pattern, BibTeX is valid `@online{}` block
    - Use `@settings(max_examples=100)`
    - **Validates: REQ-007.1, REQ-007.2, REQ-007.3**

  - [ ]* 6.5 Write property test for BibTeX aggregation (Property 9)
    - **Property 9: BibTeX aggregation completeness**
    - Generate N random citations, aggregate into references.bib, verify exactly N entries and all keys present
    - Use `@settings(max_examples=100)` with `st.lists()`
    - **Validates: REQ-007.4**

- [x] 7. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Topic coverage map (REQ-033)
  - [x] 8.1 Implement Topic Coverage Map persistence
    - Create `magic_content_engine/topic_coverage.py`
    - Load `TopicCoverageMap` from AgentCore Memory (long-term) at run start
    - Implement topic gap identification: compare niche topics against covered topics
    - Implement recommended focus derivation: gap analysis weighted by engagement signals and natural topic progression (e.g. Runtime → Gateway)
    - Implement adjacent topic tracking per `TopicCoverageEntry`
    - After content generation, update the map with topics covered in the current run (new topics added, existing topics preserved with updated dates)
    - Record Topic_Coverage_Map updates in Agent_Log
    - Handle empty map on first run (all topics listed as uncovered)
    - _Requirements: REQ-033.1, REQ-033.2, REQ-033.3, REQ-033.4, REQ-033.5, REQ-033.6_

  - [ ]* 8.2 Write property test for topic coverage map updates (Property 26)
    - **Property 26: Topic coverage map updated after each run**
    - Generate random confirmed article sets with topic tags, run update, verify new topics added and existing topics preserved with correct run dates
    - Use `@settings(max_examples=100)` with random article lists and `st.dates()`
    - **Validates: REQ-033.1, REQ-033.2, REQ-033.6**

  - [ ]* 8.3 Write property test for Weekly Brief focus derivation (Property 28)
    - **Property 28: Weekly Brief recommended focus derivation**
    - Generate coverage maps with known gaps and adjacent topic chains, verify recommended focus matches expected next topic from gap analysis
    - Use `@settings(max_examples=100)` with random `TopicCoverageMap` instances with controlled gap patterns
    - **Validates: REQ-033.4, REQ-035.2**

  - [ ]* 8.4 Write unit tests for topic coverage map
    - Test empty map on first run shows all topics as uncovered
    - Test topic gap identification with partially covered map
    - Test adjacent topic tracking (Runtime covered → Gateway recommended)
    - _Requirements: REQ-033.1, REQ-033.3, REQ-033.4_

- [ ] 9. Engagement signal tracking (REQ-034)
  - [x] 9.1 Implement dev.to API integration
    - Create `magic_content_engine/engagement.py`
    - Fetch engagement metrics (views, reactions, comments, reading_time) from dev.to API for published posts
    - Store `PostEngagement` records in AgentCore Memory (long-term), keyed by post title and publication date
    - Identify top performing post from past 7 days
    - Credential: AgentCore Identity in production, `.env` file locally
    - _Requirements: REQ-034.1, REQ-034.2, REQ-034.4, REQ-034.5_

  - [x] 9.2 Implement engagement-weighted scoring
    - Extend `scoring.py` to accept optional engagement metrics
    - When metrics exist: weight scoring toward topics that previously performed well
    - When no metrics exist (clean state): skip engagement weighting entirely, rely on relevance criteria (REQ-005) and topic gap analysis (REQ-033) only
    - _Requirements: REQ-034.3, REQ-034.10_

  - [x] 9.3 Implement clean state handling
    - When dev.to API returns no posts: treat as clean state, log "no published content yet", skip engagement tracking, no error raised
    - When dev.to API is unreachable: log failure, skip engagement tracking, continue with standard scoring
    - Record engagement metric fetches and failures in Agent_Log
    - _Requirements: REQ-034.6, REQ-034.7, REQ-034.8, REQ-034.9_

  - [ ]* 9.4 Write property test for engagement weighting disabled (Property 27)
    - **Property 27: Engagement weighting disabled until first post**
    - Generate runs with empty Engagement_Metrics, verify scoring output identical to relevance-only scoring
    - Verify Weekly_Brief omits top performing content section and shows clean state message
    - Use `@settings(max_examples=100)` with empty vs populated engagement stores
    - **Validates: REQ-034.9, REQ-034.10, REQ-035.2**

  - [ ]* 9.5 Write unit tests for engagement tracking
    - Test dev.to API returns no posts — clean state logged, no error
    - Test dev.to API unreachable — failure logged, run continues
    - Test engagement metrics stored correctly in memory
    - Test top performing post identification from past 7 days
    - _Requirements: REQ-034.6, REQ-034.7, REQ-034.8_

- [ ] 10. Weekly Brief generation (REQ-035)
  - [x] 10.1 Implement Weekly Brief generator
    - Create `magic_content_engine/weekly_brief.py`
    - Generate `WeeklyBrief` using Claude Haiku before research crawl
    - Inputs: TopicCoverageMap (long-term memory), Engagement_Metrics (long-term memory), current run date
    - Output sections:
      - Top performing content (past 7 days) — omitted if no published content, with clean state message: "No published content yet — engagement tracking will begin after your first post."
      - Topic coverage map: covered topics with most recent run date, uncovered topics as gaps
      - Recommended focus: one topic from gap analysis weighted by engagement and available articles
    - Present as clean terminal output
    - _Requirements: REQ-035.1, REQ-035.2, REQ-035.3, REQ-035.7_

  - [x] 10.2 Implement user focus override
    - User can press Enter to accept recommended focus or type a different topic
    - Store override in `WeeklyBrief.user_override`
    - Use override to weight scoring during research phase
    - Record Weekly_Brief content in Agent_Log
    - Record Weekly_Brief content and user focus selection (recommended focus + override if set) in Agent_Log
    - _Requirements: REQ-035.4, REQ-035.5, REQ-035.6_

  - [ ]* 10.3 Write unit tests for Weekly Brief
    - Test user accepts recommended focus — override is None
    - Test user overrides recommended focus — override stored and used in scoring
    - Test clean state: no engagement data → top performing section omitted, clean state message shown
    - Test Topic_Coverage_Map empty on first run → all topics listed as uncovered
    - _Requirements: REQ-035.2, REQ-035.4, REQ-035.5, REQ-033.1_

- [x] 11. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 12. User interaction, output selection, and Publish Gate
  - [x] 12.1 Implement scored article presentation and user confirmation
    - Create `magic_content_engine/user_interaction.py`
    - Present numbered list of articles scoring ≥ 3 with title, source, score, one-sentence summary
    - Wait for user confirmation; allow article removal
    - Record removals in Agent_Log
    - _Requirements: REQ-008.1, REQ-008.2, REQ-008.3, REQ-008.4_

  - [ ]* 12.2 Write property test for user removal exclusion (Property 10)
    - **Property 10: User removal exclusion**
    - Generate article lists with random removals, verify removed articles excluded from generation input and logged
    - Use `@settings(max_examples=100)`
    - **Validates: REQ-008.3**

  - [x] 12.3 Implement output choice prompt
    - Present numbered options: [1] Blog, [2] YouTube script, [3] CFP, [4] User group, [5] Digest email, [6] All
    - Unattended mode defaults to blog + YouTube (options 1 and 2)
    - _Requirements: REQ-009.1, REQ-009.2, REQ-009.3_

  - [ ]* 12.4 Write property test for output selection inclusion (Property 11)
    - **Property 11: Output selection conditional inclusion**
    - Generate random output selections, verify bundle contains exactly those files plus always-included files (references.bib, cost-estimate.txt, screenshots/, agent-log.json)
    - Use `@settings(max_examples=100)` with `st.sets(st.sampled_from(["blog","youtube","cfp","usergroup","digest"]))`
    - **Validates: REQ-009.2, REQ-017.2**

  - [x] 12.5 Implement Publish Gate review
    - Create `magic_content_engine/publish_gate.py`
    - After bundle assembly, present each Content_Output: filename, word count, first 3 lines
    - Prompt: [1] Approve, [2] Skip, [3] Hold (+ release date), [4] Review
    - Approve → add to S3 upload list
    - Skip → keep locally, log "skipped at publish gate"
    - Hold → move to `./output/held/YYYY-MM-DD-[slug]/`, store HeldItem in long-term memory
    - Review → move to `./output/review/YYYY-MM-DD-[slug]/`, log "held for manual review"
    - Unattended mode: save all locally, no auto-approve, notify via SES
    - _Requirements: REQ-030.1–REQ-030.8_

  - [ ]* 12.6 Write property test for Publish Gate completeness (Property 23)
    - **Property 23: Publish Gate completeness**
    - Generate random output sets with random gate decisions, verify only approved files in S3 upload list
    - Use `@settings(max_examples=100)` with `st.sampled_from(["approve","skip","hold","review"])`
    - **Validates: REQ-030.7**

  - [ ]* 12.7 Write property test for held item storage (Property 24)
    - **Property 24: Held item storage correctness**
    - Generate random outputs assigned Hold status with random release dates, verify HeldItem stored with correct fields and file present in held path
    - Use `@settings(max_examples=100)` with random `HeldItem` instances and `st.dates()`
    - **Validates: REQ-030.5, REQ-031.5**

  - [x] 12.8 Implement embargo release check
    - At run start, query long-term memory for HeldItems with release_date ≤ today
    - List released items to user (filename, run date, release date, path)
    - Send SES notification per released item (subject: "Magic Content Engine — embargo lifted: [title]")
    - User confirms which items to include in Publish_Gate queue
    - Approved held items: remove from memory, include in S3 upload
    - SES failure: log and continue (no retry)
    - Record all embargo checks in Agent_Log
    - _Requirements: REQ-031.1–REQ-031.6, REQ-032.1–REQ-032.5_

  - [ ]* 12.9 Write property test for auto-publish prohibition (Property 25)
    - **Property 25: Auto-publish prohibition**
    - Generate random HeldItems with past release dates, verify SES notification sent and no S3 upload without explicit approval
    - Use `@settings(max_examples=100)` with `st.dates()` and `st.booleans()` for user approval
    - **Validates: REQ-032.3, REQ-032.5**

  - [ ]* 12.10 Write unit tests for output selection and Publish Gate
    - Test output choice prompt presents options 1–6
    - Test unattended mode defaults to blog + YouTube
    - Test Publish Gate skip logs correctly
    - Test Publish Gate hold creates correct directory and memory record
    - Test Publish Gate review moves file to review directory
    - _Requirements: REQ-009.1, REQ-009.3, REQ-030.2–REQ-030.6_

- [ ] 13. Content generation — Writing_Sub_Agent
  - [x] 13.1 Implement steering file loader
    - Create `magic_content_engine/steering.py`
    - Implement `load_steering(base_path: str, output_type: str) -> dict[str, str]` per design
    - Always load `01-niche-and-voice.md`; load output-specific file based on mapping:
      - blog → `03-output-blog-post.md`
      - youtube → `04-output-youtube.md`
      - cfp → `05-output-talks.md`
      - usergroup → `05-output-talks.md`
      - digest → voice file only
    - Read from disk at invocation time, never cached or hardcoded in agent definition
    - If steering file missing: raise FileNotFoundError, abort that content output, log error
    - _Requirements: REQ-029.1, REQ-029.2, REQ-029.3, REQ-029.4, REQ-029.5_

  - [ ]* 13.2 Write property test for steering file loading (Property 22)
    - **Property 22: Steering file loading correctness**
    - For all output types, verify correct steering files loaded from disk at invocation time
    - Use `@settings(max_examples=100)` with `st.sampled_from(["blog","youtube","cfp","usergroup","digest"])`
    - **Validates: REQ-029.2, REQ-029.3**

  - [x] 13.3 Implement Writing_Sub_Agent core
    - Create `magic_content_engine/writing_agent.py`
    - Accept: articles with citations, output_type, steering_base_path, screenshots_path, run_date, slug
    - Load steering files via `load_steering()`
    - Route to Sonnet for narrative outputs (blog, youtube, cfp, usergroup), Haiku for digest
    - Apply voice rules: no banned phrases, no em-dashes, no opening with "I", short sentences
    - Insert `<!-- MIKE: [instruction, ~word count] -->` placeholder blocks
    - On generation failure: log error, continue remaining outputs
    - _Requirements: REQ-010.1, REQ-010.2, REQ-018.1–REQ-018.8, REQ-019.2, REQ-027.3_

  - [x] 13.4 Implement blog post generator
    - Generate `post.md` following `03-output-blog-post.md` structure
    - Include: hook placeholder (2-3 agent-suggested angles), architecture section with screenshot ref, build walkthrough with inline APA in-text citations and console screenshot refs, cost breakdown table, sample output section with screenshot ref, Oceania angle placeholder (~60 words), closing placeholder (~50 words, CTA, GitHub link), References section (APA citations sorted alphabetically)
    - _Requirements: REQ-011.1–REQ-011.10_

  - [x] 13.5 Implement YouTube script generator
    - Generate `script.md` and `description.txt` following `04-output-youtube.md` structure
    - Include: thumbnail concept (2-3 words + visual description), YouTube description (~150 words with hashtags #AWS #AWSCommunity #KiroIDE #AgentCore #BuildOnAWS #Aotearoa), cold open placeholder (30-45s, topic suggestion), four scripted sections (Problem ~200w, Architecture Walkthrough ~300w with B-roll cues referencing actual screenshot filenames, Build ~400w, Results+Cost ~150w), outro placeholder (~30s)
    - _Requirements: REQ-012.1–REQ-012.7_

  - [x] 13.6 Implement CFP proposal generator
    - Generate `cfp-proposal.md` following `05-output-talks.md` (CFP section)
    - Include: 3 title options (technical, community, personal story angles), abstract ≤250 words (no banned phrases), 3 specific actionable takeaways, target audience (skill level, role, prior knowledge), 25-min session outline with time allocations, 45-min variant with extended demo + Q&A, speaker bio (~100 words: AWS Community Builder AI Engineering 2026, kiro-steering-docs-extension builder, co-organiser AWS User Group Oceania, Palmerston North Aotearoa NZ), personal note placeholder, events list (AWS Summit Sydney/Auckland, AWS Community Day Oceania, KiwiCon, YOW!, DevOpsDays NZ, NDC Sydney)
    - _Requirements: REQ-013.1–REQ-013.11_

  - [x] 13.7 Implement user group session outline generator
    - Generate `usergroup-session.md` following `05-output-talks.md` (user group section)
    - Include: recommended format (lightning 10min / standard 30min / workshop 60min) with rationale, session outline (explanation + live demo + audience participation moment), step-by-step live demo instructions, slide outline (title + one-line description per slide, max 12 for 30min), opening story placeholder
    - _Requirements: REQ-014.1–REQ-014.7_

  - [x] 13.8 Implement weekly digest email generator
    - Generate `digest-email.txt` in plain-text newsletter format using Claude Haiku
    - Include: personal note placeholder (2-3 sentences), 3-4 sentences per article in plain English, group by theme when ≥ 5 articles
    - _Requirements: REQ-015.1–REQ-015.5_

  - [ ]* 13.9 Write property test for content structure completeness (Property 13)
    - **Property 13: Content output structure completeness**
    - For each output type, generate content from random article sets, verify all required sections present per design specification
    - Use `@settings(max_examples=100)` with random article sets per output type
    - **Validates: REQ-011.1–REQ-011.10, REQ-012.1–REQ-012.7, REQ-013.1–REQ-013.11, REQ-014.1–REQ-014.7, REQ-015.1–REQ-015.5**

  - [ ]* 13.10 Write property test for voice rules compliance (Property 14)
    - **Property 14: Voice rules compliance**
    - Scan all generated text for banned phrases ("leverage", "empower", "unlock", "dive into", "game-changer"), em-dash characters (— or &#8212;), paragraphs opening with "I", and placeholder format `<!-- MIKE: ... -->`
    - Use `@settings(max_examples=100)`
    - **Validates: REQ-018.3, REQ-018.4, REQ-018.5, REQ-018.8**

  - [ ]* 13.11 Write unit tests for steering file loading
    - Test missing steering file raises FileNotFoundError and aborts that output
    - Test each output type loads correct steering files from disk
    - _Requirements: REQ-029.1–REQ-029.5_

- [x] 14. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 15. Screenshot capture, output bundle, and S3 upload
  - [x] 15.1 Implement screenshot capture service
    - Create `magic_content_engine/screenshots.py`
    - Use AgentCore Browser with viewport 1440×900, wait 3s after navigation
    - Capture console screenshots: console-runtime.png, console-gateway.png, console-memory.png, console-observability.png, sample-output.png
    - Capture research screenshots for each confirmed article: `screenshots/research/YYYY-MM-DD-[source].png`
    - On failure: log (target URL + error) in Agent_Log, continue remaining captures
    - _Requirements: REQ-016.1–REQ-016.5_

  - [ ]* 15.2 Write property test for screenshot viewport configuration (Property 16)
    - **Property 16: Screenshot viewport configuration**
    - For all screenshot captures, verify viewport configured to exactly 1440×900 before capture
    - Use `@settings(max_examples=100)` with random `ScreenshotCapture` instances
    - **Validates: REQ-016.1**

  - [ ]* 15.3 Write property test for research screenshot completeness (Property 17)
    - **Property 17: Research screenshot completeness**
    - Generate random confirmed article lists, verify each has a screenshot with correct filename pattern
    - Use `@settings(max_examples=100)`
    - **Validates: REQ-016.4**

  - [x] 15.4 Implement slug generation
    - Create `magic_content_engine/slug.py`
    - Derive kebab-case slug from primary topic of confirmed article list
    - Slug: lowercase alphanumeric + hyphens only, no leading/trailing/consecutive hyphens
    - Directory name: `YYYY-MM-DD-[slug]`
    - _Requirements: REQ-028.1, REQ-028.2, REQ-028.3_

  - [x] 15.5 Implement output bundle assembler
    - Create `magic_content_engine/bundle.py`
    - Assemble directory: `output/YYYY-MM-DD-[slug]/` with selected content files + always-included files (references.bib, cost-estimate.txt, screenshots/, agent-log.json)
    - Generate cost-estimate.txt: per-task breakdown by model, token count (input/output), estimated cost using Bedrock pricing
    - Generate agent-log.json: invocation source, articles found/kept, scores, models per task, screenshot results, errors, selected outputs, run metadata
    - _Requirements: REQ-017.1–REQ-017.4, REQ-026.1–REQ-026.3_

  - [x] 15.6 Implement S3 upload with retry
    - Upload only Publish_Gate-approved files to configured S3 bucket under `output/YYYY-MM-DD-[slug]/`
    - Retry up to 3 times with exponential backoff (1s, 2s, 4s)
    - Log failure after exhausting retries
    - Skip upload entirely if no files approved
    - _Requirements: REQ-017.5, REQ-024.1, REQ-024.2_

  - [ ]* 15.7 Write property test for cost estimation correctness (Property 20)
    - **Property 20: Cost estimation correctness**
    - Generate random token counts and model IDs, verify cost calculation (total = sum of individual invocation costs + AgentCore costs)
    - Use `@settings(max_examples=100)` with `st.integers()` for tokens
    - **Validates: REQ-026.1, REQ-026.2, REQ-026.3**

  - [ ]* 15.8 Write property test for agent log completeness (Property 19)
    - **Property 19: Agent log completeness**
    - Generate random run results, verify log contains all required fields: invocation source, article counts, scores, models, screenshot results, errors, selected outputs
    - Use `@settings(max_examples=100)`
    - **Validates: REQ-001.3, REQ-005.4, REQ-010.3, REQ-017.4**

  - [ ]* 15.9 Write unit tests for screenshot capture and bundle assembly
    - Test console screenshot filenames match expected list
    - Test S3 upload to correct key prefix
    - Test S3 retry with exponential backoff
    - Test screenshot failure logging
    - _Requirements: REQ-016.3, REQ-016.5, REQ-017.5, REQ-024.2_

- [x] 16. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 17. Orchestrator workflow, observability, and terminal summary
  - [x] 17.1 Implement Orchestrator_Agent end-to-end workflow
    - Create `magic_content_engine/orchestrator.py` (extend from phase 1)
    - Wire all components in sequence:
      1. Accept trigger (EventBridge scheduled or manual CLI)
      2. Load voice profile + previously covered URLs from long-term memory
      3. Fetch engagement metrics from dev.to API (REQ-034)
      4. Load Topic_Coverage_Map from long-term memory (REQ-033)
      5. Generate and present Weekly_Brief (REQ-035), accept user focus override
      6. Check for released HeldItems, send SES notifications (REQ-031, REQ-032)
      7. Crawl primary sources (5) and secondary sources (4)
      8. Deduplicate against long-term memory
      9. Score articles via Haiku, exclude below threshold
      10. Extract metadata and build APA citations
      11. Present scored articles, wait for user confirmation
      12. Present output choice prompt
      13. Invoke Writing_Sub_Agent per selected output
      14. Update Topic_Coverage_Map with topics covered this run
      15. Capture screenshots
      16. Assemble output bundle
      17. Run Publish_Gate review
      18. Upload approved files to S3
      19. Store confirmed article URLs in long-term memory
      20. Print terminal summary
    - Record invocation source in Agent_Log
    - _Requirements: REQ-001.1, REQ-001.2, REQ-001.3, REQ-019.1, REQ-019.3_

  - [ ]* 17.2 Write property test for manual/scheduled equivalence (Property 1)
    - **Property 1: Manual and scheduled invocations produce equivalent workflows**
    - Generate random trigger payloads with both sources, verify same workflow steps execute and structurally equivalent bundles produced
    - Use `@settings(max_examples=100)` with `st.sampled_from(["scheduled", "manual"])`
    - **Validates: REQ-001.2**

  - [x] 17.3 Implement AgentCore Observability integration
    - Emit trace spans for each major workflow step: crawling, scoring, metadata extraction, citation building, user interaction, content generation, screenshot capture, bundle assembly
    - Record per-step latency in trace spans
    - Emit error-level trace events on failures (step name, error message, context)
    - _Requirements: REQ-023.1, REQ-023.2, REQ-023.3_

  - [ ]* 17.4 Write property test for observability trace completeness (Property 18)
    - **Property 18: Observability trace completeness**
    - For each workflow step, verify trace span emitted with latency duration
    - Use `@settings(max_examples=100)` with `st.sampled_from(workflow_steps)`
    - **Validates: REQ-023.1, REQ-023.3**

  - [x] 17.5 Implement terminal summary
    - Print at run end: total articles found, articles kept after scoring, content outputs generated, estimated run cost (AgentCore + LLM tokens), failed screenshot captures (if any)
    - Indicate clean run status when no errors
    - Include all logged errors
    - _Requirements: REQ-025.1, REQ-025.2, REQ-027.5_

  - [ ]* 17.6 Write property test for terminal summary completeness (Property 21)
    - **Property 21: Terminal summary completeness**
    - Generate random run results, verify summary contains all required fields
    - Use `@settings(max_examples=100)`
    - **Validates: REQ-025.1**

  - [x] 17.7 Implement AgentCore Gateway external tool registration
    - Register `invoke_content_run` as MCP tool through AgentCore Gateway for external callers
    - Internal Strands tool calls (crawling, scoring, citation, screenshot, file-writing) use SDK directly, NOT through Gateway
    - _Requirements: REQ-020.1, REQ-020.2_

  - [x] 17.8 Implement AgentCore Identity integration
    - GitHub API authentication via AgentCore Identity in production
    - dev.to API authentication via AgentCore Identity in production
    - Local development: read from `.env` file (excluded via `.gitignore`)
    - YouTube Data API upload out of scope for v1
    - _Requirements: REQ-021.1, REQ-021.2, REQ-021.3_

  - [x] 17.9 Implement AgentCore Memory session and long-term usage
    - Short-term memory: current article list, scoring progress, selected outputs during a run
    - Long-term memory: previously covered article URLs, run dates, voice profile, content preferences, TopicCoverageMap, Engagement_Metrics, HeldItems
    - Load voice profile and content preferences at run start
    - _Requirements: REQ-022.1, REQ-022.2, REQ-022.3_

  - [ ]* 17.10 Write unit tests for orchestrator workflow
    - Test scheduled and manual invocations execute same workflow
    - Test invocation source recorded in Agent_Log
    - Test clean run status indicated when no errors
    - Test all errors included in Agent_Log and terminal summary
    - _Requirements: REQ-001.1–REQ-001.3, REQ-025.2, REQ-027.5_

- [x] 18. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- All 28 correctness properties from the design are covered as property-based test sub-tasks
- All 35 requirements (REQ-001 through REQ-035) are covered by implementation tasks
- Property-based tests use Hypothesis with `@settings(max_examples=100)`
- Checkpoints at phases 3, 7, 11, 14, 16, and 18 ensure incremental validation
- Python with Strands Agents SDK; Claude Haiku for structured tasks, Claude Sonnet for narrative writing
- Voice rules loaded at runtime from `.kiro/steering/01-niche-and-voice.md`
- Error handling follows log-and-continue strategy throughout
- S3 retry: exponential backoff (1s, 2s, 4s); SES failures: logged without retry
- AgentCore Gateway is for exposing tools to external callers via MCP, not for routing internal function calls
- YouTube Data API upload is out of scope for v1
- Primary sources: 5; Secondary sources: 4
