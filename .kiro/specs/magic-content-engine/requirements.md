# Requirements Document

## Introduction

The Magic Content Engine is a scheduled, AWS-hosted agent application built with the Strands Agents SDK (Python). It crawls AWS and Kiro IDE news sources on a weekly cadence, scores articles by relevance to a specific niche (AI Engineering tooling on AWS using Kiro IDE, from the Oceania/Pacific community perspective), builds APA 7th edition citations, captures screenshots, and generates a selectable set of content outputs (blog post, YouTube script, CFP proposal, user group session outline, weekly digest email). The system uses a cost-optimised model routing strategy (Claude Haiku for structured tasks, Claude Sonnet for narrative writing) and produces a structured output bundle stored in S3. Voice rules and niche context are governed by the workspace steering file at `.kiro/steering/01-niche-and-voice.md`.

## Glossary

- **Orchestrator_Agent**: The primary Strands agent hosted on AgentCore Runtime that coordinates research, scoring, citation building, screenshot capture, content generation, and output bundle assembly.
- **Writing_Sub_Agent**: A secondary Strands agent invoked by the Orchestrator_Agent for each selected content output type, receiving relevant context and producing formatted content.
- **Research_Source**: A URL endpoint (primary or secondary) that the system crawls to discover articles and announcements.
- **Article**: A single news item, blog post, changelog entry, GitHub issue, or release discovered during a research crawl.
- **Relevance_Score**: An integer from 1 to 5 assigned to each Article by Claude Haiku, indicating how relevant the Article is to the content niche.
- **APA_Citation**: A bibliographic reference formatted according to APA 7th edition rules, constructed from metadata extracted from a source page.
- **Output_Bundle**: The complete directory of generated files, screenshots, references, cost estimate, and agent log produced by a single run.
- **Content_Output**: One of five selectable output types: blog post, YouTube script, CFP proposal, user group session outline, or weekly digest email. YouTube script generation produces a local file only; YouTube upload is not automated in v1.
- **Screenshot**: A PNG image captured by AgentCore Browser at 1440×900 viewport resolution.
- **Voice_Rules**: The set of writing style constraints defined in `.kiro/steering/01-niche-and-voice.md` that govern all generated text.
- **Placeholder**: A HTML comment block in the format `<!-- MIKE: [instruction, ~word count] -->` reserved for the content owner to write personally.
- **Output_Choice_Prompt**: The interactive step where the system presents the user with a numbered list of Content_Output types and waits for selection before generating.
- **Agent_Log**: A JSON file recording articles found, scores assigned, models used per step, and run metadata.
- **Cost_Estimate**: A text file containing a per-service and per-model token cost breakdown for the current run.
- **Publish_Gate**: The end-of-run review step where the user reviews each generated Content_Output and assigns a publish status before any content leaves the local output bundle.
- **Held_Item**: A generated Content_Output that has been assigned an embargo release date and is stored locally in `./output/held/` pending the release date.
- **Topic_Coverage_Map**: A persistent record stored in AgentCore Memory (long-term) tracking which niche topics have been covered in previous runs, associated article titles and run dates, and identified topic gaps.
- **Engagement_Metrics**: Per-post performance data retrieved from the dev.to API, including page views, reactions, comments, and reading time, stored in AgentCore Memory (long-term) to inform future content scoring.
- **Weekly_Brief**: A personalised summary generated at the start of each run presenting top performing content, topic coverage gaps, and a recommended focus topic for the current run.

## Requirements

### Requirement 1: Weekly Scheduled Trigger

**User Story:** As a content creator, I want the engine to run automatically on a weekly schedule, so that I receive fresh research and content without manual intervention.

#### Acceptance Criteria

1. THE Orchestrator_Agent SHALL accept invocation from an Amazon EventBridge scheduled rule on a configurable weekly cadence.
2. WHEN invoked manually (outside the EventBridge schedule), THE Orchestrator_Agent SHALL execute the same research and generation workflow as a scheduled run.
3. THE Orchestrator_Agent SHALL record the invocation source (scheduled or manual) in the Agent_Log.

### Requirement 2: Primary Source Crawling

**User Story:** As a content creator, I want the engine to crawl all primary research sources every run, so that I have comprehensive coverage of the most important news.

#### Acceptance Criteria

1. WHEN a run begins, THE Orchestrator_Agent SHALL crawl all five primary Research_Sources using AgentCore Browser:
   - kiro.dev/changelog/ide/
   - github.com/kirodotdev/Kiro/issues
   - aws.amazon.com/new/ (filtered to: bedrock, agentcore, kiro, lambda)
   - aws.amazon.com/blogs/machine-learning/
   - community.aws/
2. WHEN crawling aws.amazon.com/new/, THE Orchestrator_Agent SHALL apply keyword filters for "bedrock", "agentcore", "kiro", and "lambda" to restrict results.
3. IF a primary Research_Source is unreachable after three retry attempts, THEN THE Orchestrator_Agent SHALL log the failure in the Agent_Log and continue crawling remaining sources.

### Requirement 3: Secondary Source Crawling

**User Story:** As a content creator, I want the engine to crawl secondary sources weekly, so that I also capture broader ecosystem updates.

#### Acceptance Criteria

1. WHEN a weekly run executes, THE Orchestrator_Agent SHALL crawl all four secondary Research_Sources using AgentCore Browser:
   - github.com/awslabs/ (new AgentCore and Strands releases)
   - strandsagents.com
   - repost.aws/ (trending AI/ML)
   - kiro.dev/blog/
2. IF a secondary Research_Source is unreachable after three retry attempts, THEN THE Orchestrator_Agent SHALL log the failure in the Agent_Log and continue crawling remaining sources.

### Requirement 4: Duplicate Article Detection

**User Story:** As a content creator, I want the engine to skip articles it has already covered in previous runs, so that I only see new material.

#### Acceptance Criteria

1. WHEN an Article is discovered during crawling, THE Orchestrator_Agent SHALL query AgentCore Memory (long-term) to determine whether the Article URL has been processed in a previous run.
2. WHEN an Article URL matches a previously processed record in AgentCore Memory, THE Orchestrator_Agent SHALL exclude that Article from scoring and mark it as "previously covered" in the Agent_Log.
3. WHEN an Article passes scoring and is confirmed by the user, THE Orchestrator_Agent SHALL store the Article URL and run date in AgentCore Memory (long-term).

### Requirement 5: Relevance Scoring

**User Story:** As a content creator, I want each discovered article scored by relevance to my niche, so that I only spend time on material that matters.

#### Acceptance Criteria

1. WHEN Articles are discovered from Research_Sources, THE Orchestrator_Agent SHALL send each Article to Claude Haiku for Relevance_Score assignment on a 1-to-5 integer scale.
2. THE Orchestrator_Agent SHALL apply the following scoring criteria:
   - High (4-5): Kiro IDE features or breaking changes, AgentCore/Strands/Bedrock announcements, MCP spec updates, steering docs or Kiro extension ecosystem news, Community Builder programme news, changes affecting kiro-steering-docs-extension directly.
   - Medium (3): AWS Lambda/S3/IAM changes affecting agent deployments, general agentic AI patterns with AWS application, NZ/Oceania AWS events or community news.
   - Low (1-2): Generic AI news without AWS angle, AWS services with no agent relevance.
3. WHEN an Article receives a Relevance_Score below 3, THE Orchestrator_Agent SHALL exclude that Article from further processing.
4. THE Orchestrator_Agent SHALL record each Article's Relevance_Score and scoring rationale in the Agent_Log.

### Requirement 6: Metadata Extraction

**User Story:** As a content creator, I want structured metadata extracted from each source page, so that citations and references are accurate.

#### Acceptance Criteria

1. WHEN an Article passes relevance scoring, THE Orchestrator_Agent SHALL extract the following metadata using Claude Haiku:
   - Article title (from og:title or HTML title element)
   - Publication date (from og:published_time or article:published_time)
   - Author name (from author meta tag or visible byline)
   - Publisher or site name (from og:site_name)
   - Canonical URL
2. WHEN author metadata is not available on the source page, THE Orchestrator_Agent SHALL use "Amazon Web Services" as the fallback author.
3. WHEN publisher metadata is not available on the source page, THE Orchestrator_Agent SHALL use "Amazon Web Services" as the fallback publisher.

### Requirement 7: APA 7th Edition Citation Building

**User Story:** As a content creator, I want properly formatted APA 7th edition citations for every source, so that my published content meets academic referencing standards.

#### Acceptance Criteria

1. WHEN metadata has been extracted for an Article, THE Orchestrator_Agent SHALL construct an APA 7th edition reference entry using Claude Haiku in the format: Author, A. A. (Year, Month Day). *Title*. Site Name. URL
2. THE Orchestrator_Agent SHALL construct an in-text citation in the format: (Surname, Year) or (Amazon Web Services, Year) when the fallback author is used.
3. THE Orchestrator_Agent SHALL generate a BibTeX entry for each APA_Citation.
4. THE Orchestrator_Agent SHALL save all BibTeX entries to a references.bib file in the Output_Bundle.
5. FOR ALL valid APA_Citations, formatting the citation then parsing the formatted output SHALL produce metadata equivalent to the original extracted metadata (round-trip property).

### Requirement 8: User Confirmation of Scored Articles

**User Story:** As a content creator, I want to review and confirm the scored article list before content generation begins, so that I control what gets written about.

#### Acceptance Criteria

1. WHEN scoring is complete, THE Orchestrator_Agent SHALL present the user with a numbered list of Articles that scored 3 or above, showing each Article's title, source, Relevance_Score, and a one-sentence summary.
2. WHEN the scored list is presented, THE Orchestrator_Agent SHALL wait for user confirmation before proceeding.
3. WHEN the user removes Articles from the list, THE Orchestrator_Agent SHALL exclude those Articles from content generation and record the removal in the Agent_Log.
4. WHEN the user confirms the list, THE Orchestrator_Agent SHALL proceed to the Output_Choice_Prompt.

### Requirement 9: Output Selection Prompt

**User Story:** As a content creator, I want to choose which content outputs to generate each run, so that I only produce what I need.

#### Acceptance Criteria

1. WHEN the user confirms the scored article list, THE Orchestrator_Agent SHALL present the Output_Choice_Prompt with the following numbered options:
   - [1] Blog post
   - [2] YouTube script
   - [3] CFP proposal
   - [4] User group session outline
   - [5] Weekly digest email
   - [6] All of the above
2. WHEN the user selects one or more options, THE Orchestrator_Agent SHALL generate only the selected Content_Outputs.
3. WHEN the run is unattended or automated (no user interaction available), THE Orchestrator_Agent SHALL default to generating the blog post and YouTube script (options 1 and 2).

### Requirement 10: Model Routing Strategy

**User Story:** As a content creator, I want the engine to use cost-effective models for structured tasks and higher-capability models for writing, so that I optimise cost without sacrificing quality.

#### Acceptance Criteria

1. THE Orchestrator_Agent SHALL route tasks to Claude Haiku for: relevance scoring, APA citation formatting, metadata extraction, and digest email generation.
2. THE Orchestrator_Agent SHALL route tasks to Claude Sonnet for: blog post writing, YouTube script writing, CFP abstract writing, and user group session outline writing.
3. THE Orchestrator_Agent SHALL record the model used for each task in the Agent_Log.

### Requirement 11: Blog Post Generation

**User Story:** As a content creator, I want a dev.to-formatted blog post with inline citations and screenshots, so that I have a near-publishable draft.

#### Acceptance Criteria

1. WHEN the blog post output is selected, THE Writing_Sub_Agent SHALL generate a Markdown file following the structure defined in `.kiro/steering/03-output-blog-post.md`.
2. THE Writing_Sub_Agent SHALL include a hook Placeholder with 2-3 agent-suggested hook angles based on the top-scored Articles.
3. THE Writing_Sub_Agent SHALL include an architecture section with an embedded screenshot reference.
4. THE Writing_Sub_Agent SHALL include a build walkthrough section with inline APA in-text citations and console screenshot references.
5. THE Writing_Sub_Agent SHALL include a cost breakdown table with per-service cost estimates.
6. THE Writing_Sub_Agent SHALL include a sample output section with a screenshot reference.
7. THE Writing_Sub_Agent SHALL include an Oceania angle Placeholder (approximately 60 words).
8. THE Writing_Sub_Agent SHALL include a closing Placeholder (approximately 50 words, CTA, GitHub link).
9. THE Writing_Sub_Agent SHALL include a References section with all APA_Citations sorted alphabetically by author surname.
10. THE Writing_Sub_Agent SHALL save the blog post as `post.md` in the Output_Bundle directory.

### Requirement 12: YouTube Script Generation

**User Story:** As a content creator, I want a structured YouTube script with B-roll cues and a separate description file, so that I can efficiently produce video content.

#### Acceptance Criteria

1. WHEN the YouTube script output is selected, THE Writing_Sub_Agent SHALL generate a Markdown file following the structure defined in `.kiro/steering/04-output-youtube.md`.
2. THE Writing_Sub_Agent SHALL include a thumbnail concept (2-3 words plus a one-line visual description).
3. THE Writing_Sub_Agent SHALL include a YouTube description of approximately 150 words with hashtags: #AWS #AWSCommunity #KiroIDE #AgentCore #BuildOnAWS #Aotearoa.
4. THE Writing_Sub_Agent SHALL include a cold open Placeholder (30-45 seconds, to camera, no script) with a topic suggestion based on the top-scored Article.
5. THE Writing_Sub_Agent SHALL include four scripted sections: The Problem (approximately 200 words), Architecture Walkthrough (approximately 300 words with B-roll cues referencing actual screenshot filenames), The Build (approximately 400 words), and Results plus Cost (approximately 150 words).
6. THE Writing_Sub_Agent SHALL include an outro Placeholder (approximately 30 seconds, to camera).
7. THE Writing_Sub_Agent SHALL save the script as `script.md` and the YouTube description as `description.txt` in the Output_Bundle directory.

### Requirement 13: CFP Proposal Generation

**User Story:** As a content creator, I want a conference talk proposal with multiple title options and session outlines, so that I can submit to relevant events quickly.

#### Acceptance Criteria

1. WHEN the CFP proposal output is selected, THE Writing_Sub_Agent SHALL generate a Markdown file following the structure defined in `.kiro/steering/05-output-talks.md` (CFP section).
2. THE Writing_Sub_Agent SHALL include three title options: one technical angle, one community angle, and one personal story angle.
3. THE Writing_Sub_Agent SHALL include an abstract of 250 words maximum, containing no banned phrases (leverage, empower, unlock, dive into, game-changer).
4. THE Writing_Sub_Agent SHALL include three specific, actionable key takeaways.
5. THE Writing_Sub_Agent SHALL include a target audience description specifying skill level, role, and prior knowledge assumed.
6. THE Writing_Sub_Agent SHALL include a 25-minute session outline with time allocations per section.
7. THE Writing_Sub_Agent SHALL include a 45-minute variant outline with extended demo time and Q&A.
8. THE Writing_Sub_Agent SHALL include a speaker bio draft of approximately 100 words referencing: AWS Community Builder AI Engineering 2026, builder of kiro-steering-docs-extension, co-organiser AWS User Group Oceania, Palmerston North Aotearoa NZ.
9. THE Writing_Sub_Agent SHALL include a personal note Placeholder for the speaker.
10. THE Writing_Sub_Agent SHALL include a list of suitable events: AWS Summit Sydney/Auckland, AWS Community Day Oceania, KiwiCon, YOW! Conference, DevOpsDays NZ, NDC Sydney.
11. THE Writing_Sub_Agent SHALL save the proposal as `cfp-proposal.md` in the Output_Bundle directory.

### Requirement 14: User Group Session Outline Generation

**User Story:** As a content creator, I want a user group session outline with live demo steps and slide structure, so that I can prepare community presentations efficiently.

#### Acceptance Criteria

1. WHEN the user group session outline output is selected, THE Writing_Sub_Agent SHALL generate a Markdown file following the structure defined in `.kiro/steering/05-output-talks.md` (user group section).
2. THE Writing_Sub_Agent SHALL include a recommended format (lightning 10 minutes, standard 30 minutes, or workshop 60 minutes) with a rationale for the recommendation.
3. THE Writing_Sub_Agent SHALL include a session outline designed for a community audience, mixing explanation, live demo, and one audience participation moment.
4. THE Writing_Sub_Agent SHALL include step-by-step live demo instructions that are followable on stage.
5. THE Writing_Sub_Agent SHALL include a slide outline with a title and one-line content description per slide, with a maximum of 12 slides for a 30-minute session.
6. THE Writing_Sub_Agent SHALL include an opening story Placeholder.
7. THE Writing_Sub_Agent SHALL save the outline as `usergroup-session.md` in the Output_Bundle directory.

### Requirement 15: Weekly Digest Email Generation

**User Story:** As a content creator, I want a plain-text weekly digest email summarising the top articles, so that I can share updates with my community.

#### Acceptance Criteria

1. WHEN the weekly digest email output is selected, THE Writing_Sub_Agent SHALL generate a plain-text file in newsletter format using Claude Haiku.
2. THE Writing_Sub_Agent SHALL write 3-4 sentences per Article in plain English.
3. THE Writing_Sub_Agent SHALL include a personal note Placeholder at the top of the email (2-3 sentences).
4. WHEN the digest contains 5 or more Articles, THE Writing_Sub_Agent SHALL group Articles by theme.
5. THE Writing_Sub_Agent SHALL save the digest as `digest-email.txt` in the Output_Bundle directory.

### Requirement 16: Screenshot Capture

**User Story:** As a content creator, I want automated screenshots of source pages and AWS console views, so that my content includes accurate visual references.

#### Acceptance Criteria

1. THE Orchestrator_Agent SHALL capture all Screenshots using AgentCore Browser with a viewport resolution of 1440 by 900 pixels.
2. WHEN navigating to a page for screenshot capture, THE Orchestrator_Agent SHALL wait a minimum of 3 seconds after navigation before capturing, to allow React-based pages to render.
3. THE Orchestrator_Agent SHALL capture the following console Screenshots and save them to the screenshots directory in the Output_Bundle:
   - console-runtime.png (AgentCore Runtime dashboard)
   - console-gateway.png (AgentCore Gateway tool list)
   - console-memory.png (AgentCore Memory records)
   - console-observability.png (AgentCore Observability trace)
   - sample-output.png (generated digest rendered as HTML)
4. WHEN an Article passes scoring and is confirmed by the user, THE Orchestrator_Agent SHALL capture a Screenshot of the Article's source landing page and save it as `screenshots/research/YYYY-MM-DD-[source].png`.
5. IF a Screenshot capture fails, THEN THE Orchestrator_Agent SHALL log the failure (including the target URL and error) in the Agent_Log and continue processing.

### Requirement 17: Output Bundle Assembly

**User Story:** As a content creator, I want all generated content, references, screenshots, and metadata assembled into a single structured directory, so that I have everything in one place.

#### Acceptance Criteria

1. WHEN content generation and screenshot capture are complete, THE Orchestrator_Agent SHALL assemble the Output_Bundle in the directory structure: `output/YYYY-MM-DD-[slug]/`.
2. THE Output_Bundle SHALL contain only the Content_Output files that were selected by the user, plus the following files that are always included: references.bib, cost-estimate.txt, the screenshots directory, and agent-log.json.
3. THE Orchestrator_Agent SHALL generate a cost-estimate.txt file containing a per-service and per-model token cost breakdown for the current run.
4. THE Orchestrator_Agent SHALL generate an agent-log.json file containing: articles found, articles kept after scoring, Relevance_Scores, model used per task, screenshot capture results, and run metadata.
5. THE Orchestrator_Agent SHALL upload only the Publish_Gate-approved files from the Output_Bundle to S3, following completion of the Publish_Gate review in REQ-030.

### Requirement 18: Voice Rules Enforcement

**User Story:** As a content creator, I want all generated text to follow my established voice rules, so that the output sounds like me and fits my brand.

#### Acceptance Criteria

1. THE Writing_Sub_Agent SHALL apply the Voice_Rules defined in `.kiro/steering/01-niche-and-voice.md` to all generated text content.
2. THE Writing_Sub_Agent SHALL use conversational, first-person tone and short sentences in all generated text.
3. THE Writing_Sub_Agent SHALL not use any of the following banned phrases in generated text: "leverage", "empower", "unlock", "dive into", "game-changer".
4. THE Writing_Sub_Agent SHALL not use em-dashes in generated text.
5. THE Writing_Sub_Agent SHALL not open any paragraph or section with the word "I".
6. THE Writing_Sub_Agent SHALL reference Oceania and Pacific community context naturally where relevant, without forced or performative inclusion.
7. THE Writing_Sub_Agent SHALL use "Kia ora" or "Ngā mihi" in content intended for NZ or government-facing audiences.
8. THE Writing_Sub_Agent SHALL leave all Placeholder blocks empty, containing only the instruction comment for the content owner.

### Requirement 19: AgentCore Runtime Integration

**User Story:** As a developer, I want the orchestrator and writing agents hosted on AgentCore Runtime, so that the system runs as managed, production-grade agents.

#### Acceptance Criteria

1. THE Orchestrator_Agent SHALL be deployed and hosted on AgentCore Runtime using the Strands Agents SDK (Python).
2. THE Writing_Sub_Agent SHALL be deployed and hosted on AgentCore Runtime using the Strands Agents SDK (Python).
3. WHEN the Orchestrator_Agent needs to invoke the Writing_Sub_Agent, THE Orchestrator_Agent SHALL pass the confirmed Article list, selected Content_Output type, APA_Citations, and relevant context to the Writing_Sub_Agent.

### Requirement 20: AgentCore Gateway External Tool Exposure

**User Story:** As a developer, I want the agent's capabilities exposed via AgentCore Gateway as MCP tools, so that external callers and other agents can invoke content runs through a managed interface.

#### Acceptance Criteria

1. THE Orchestrator_Agent SHALL register externally accessible capabilities (e.g., `invoke_content_run`) as MCP tools through AgentCore Gateway.
2. Internal Strands tool calls within the Orchestrator_Agent (crawling, scoring, citation, screenshot, file-writing) SHALL use the Strands SDK directly and SHALL NOT route through AgentCore Gateway.

### Requirement 21: AgentCore Identity for Outbound Authentication

**User Story:** As a developer, I want the agent to authenticate with external APIs securely, so that GitHub API calls succeed without hardcoded credentials.

#### Acceptance Criteria

1. WHEN the Orchestrator_Agent accesses the GitHub API, THE Orchestrator_Agent SHALL authenticate using credentials managed by AgentCore Identity.
2. IN production deployments, THE Orchestrator_Agent SHALL not store API keys, tokens, or secrets in source code or configuration files committed to version control. DURING local development and testing, THE Orchestrator_Agent MAY read credentials from environment variables or a `.env` file that is excluded from version control via `.gitignore`.
3. YouTube Data API integration is out of scope for v1. The YouTube script output (REQ-012) generates local files only.

### Requirement 22: AgentCore Memory Usage

**User Story:** As a developer, I want the agent to use short-term and long-term memory, so that it tracks session state and remembers previously covered articles across runs.

#### Acceptance Criteria

1. THE Orchestrator_Agent SHALL use AgentCore Memory (short-term) to maintain session state during a single run, including the current article list, scoring progress, and selected outputs.
2. THE Orchestrator_Agent SHALL use AgentCore Memory (long-term) to store previously covered Article URLs, run dates, the content owner's voice profile, and content preferences.
3. WHEN a new run begins, THE Orchestrator_Agent SHALL load the voice profile and content preferences from AgentCore Memory (long-term) to inform scoring and generation.

### Requirement 23: AgentCore Observability

**User Story:** As a developer, I want full tracing and monitoring of agent runs, so that I can debug failures and understand performance.

#### Acceptance Criteria

1. THE Orchestrator_Agent SHALL emit trace spans to AgentCore Observability for each major workflow step: crawling, scoring, metadata extraction, citation building, user interaction, content generation, screenshot capture, and bundle assembly.
2. WHEN an error occurs during any workflow step, THE Orchestrator_Agent SHALL emit an error-level trace event to AgentCore Observability containing the step name, error message, and relevant context.
3. THE Orchestrator_Agent SHALL record per-step latency in trace spans sent to AgentCore Observability.

### Requirement 24: S3 Output Storage

**User Story:** As a content creator, I want the output bundle uploaded to S3, so that I can access generated content from anywhere.

#### Acceptance Criteria

1. WHEN the Output_Bundle is fully assembled, THE Orchestrator_Agent SHALL upload the complete Output_Bundle directory to a configured S3 bucket under the key prefix `output/YYYY-MM-DD-[slug]/`.
2. IF the S3 upload fails, THEN THE Orchestrator_Agent SHALL retry the upload up to three times with exponential backoff before logging the failure in the Agent_Log.

### Requirement 25: Terminal Summary

**User Story:** As a content creator, I want a summary printed at the end of each run, so that I can quickly see what happened without reading logs.

#### Acceptance Criteria

1. WHEN a run completes, THE Orchestrator_Agent SHALL print a terminal summary containing:
   - Total number of Articles found during crawling.
   - Number of Articles kept after relevance scoring.
   - List of Content_Outputs generated.
   - Estimated run cost (AgentCore service costs plus LLM token costs).
   - List of Screenshots that failed to capture (if any).
2. WHEN all operations succeed with no failures, THE Orchestrator_Agent SHALL indicate a clean run status in the terminal summary.

### Requirement 26: Cost Estimation

**User Story:** As a content creator, I want a per-run cost breakdown, so that I can track and optimise spending.

#### Acceptance Criteria

1. THE Orchestrator_Agent SHALL track token usage (input and output tokens) for each model invocation during a run.
2. THE Orchestrator_Agent SHALL calculate estimated cost per model invocation using current Bedrock pricing for Claude Haiku and Claude Sonnet.
3. THE Orchestrator_Agent SHALL generate a cost-estimate.txt file in the Output_Bundle containing a breakdown by task type, model used, token count, and estimated cost.

### Requirement 27: Graceful Error Handling

**User Story:** As a developer, I want the engine to handle errors gracefully and continue processing where possible, so that a single failure does not abort the entire run.

#### Acceptance Criteria

1. IF a single Research_Source crawl fails, THEN THE Orchestrator_Agent SHALL log the error and continue crawling remaining sources.
2. IF metadata extraction fails for a single Article, THEN THE Orchestrator_Agent SHALL log the error, skip that Article, and continue processing remaining Articles.
3. IF a Content_Output generation fails, THEN THE Orchestrator_Agent SHALL log the error and continue generating remaining selected Content_Outputs.
4. IF a Screenshot capture fails, THEN THE Orchestrator_Agent SHALL log the failure and continue with remaining captures.
5. THE Orchestrator_Agent SHALL include all logged errors in the Agent_Log and the terminal summary.

### Requirement 28: Slug Generation for Output Directories

**User Story:** As a content creator, I want output directories named with a date and descriptive slug, so that bundles are easy to identify and sort.

#### Acceptance Criteria

1. WHEN assembling the Output_Bundle, THE Orchestrator_Agent SHALL generate a kebab-case slug derived from the primary topic of the confirmed Article list.
2. THE Orchestrator_Agent SHALL name the Output_Bundle directory using the format `YYYY-MM-DD-[slug]` where YYYY-MM-DD is the run date.
3. THE Orchestrator_Agent SHALL ensure the slug contains only lowercase alphanumeric characters and hyphens.

### Requirement 29: Runtime Steering File Loading

**User Story:** As a developer, I want the writing agent to read steering files from disk at runtime rather than having their contents baked into the agent's system prompt, so that voice rules and output templates can be updated without redeploying the agent.

#### Acceptance Criteria

1. WHEN the Writing_Sub_Agent is invoked for any Content_Output, THE Writing_Sub_Agent SHALL read the applicable `.kiro/steering/` files from the filesystem at runtime before generating content.
2. THE Writing_Sub_Agent SHALL load `.kiro/steering/01-niche-and-voice.md` for every content generation invocation to apply current Voice_Rules.
3. THE Writing_Sub_Agent SHALL load the output-specific steering file for the selected Content_Output type (e.g., `03-output-blog-post.md` for blog posts, `04-output-youtube.md` for YouTube scripts, `05-output-talks.md` for CFP proposals and user group sessions).
4. THE Writing_Sub_Agent SHALL NOT have steering file contents hardcoded or embedded in its system prompt or agent definition.
5. IF a referenced steering file is missing or unreadable at runtime, THEN THE Writing_Sub_Agent SHALL log the error in the Agent_Log and abort generation for that Content_Output.

### Requirement 30: Publish Gate Review

**User Story:** As a content creator, I want to review all generated content for NDA and embargo status after generation but before publishing, so that nothing leaves my local machine without my explicit approval.

#### Acceptance Criteria

1. AFTER the Output_Bundle is fully assembled and BEFORE any content is uploaded to S3 or published externally, THE Orchestrator_Agent SHALL present the Publish_Gate for each generated Content_Output file.
2. FOR each Content_Output, THE Orchestrator_Agent SHALL display the filename, word count, and first 3 lines of content, then prompt the user:
   - [1] Approve — include in S3 upload
   - [2] Skip — exclude from S3 upload, keep locally only
   - [3] Hold — embargoed, enter release date (YYYY-MM-DD)
   - [4] Review — move to `./output/review/` for manual inspection
3. WHEN the user selects [1] Approve, THE Orchestrator_Agent SHALL include that file in the S3 upload.
4. WHEN the user selects [2] Skip, THE Orchestrator_Agent SHALL exclude that file from the S3 upload and record it as "skipped at publish gate" in the Agent_Log.
5. WHEN the user selects [3] Hold, THE Orchestrator_Agent SHALL move the Content_Output to `./output/held/YYYY-MM-DD-[slug]/` and store a Held_Item record in AgentCore Memory (long-term) containing: filename, S3 destination path, release date, article titles covered, and run date.
6. WHEN the user selects [4] Review, THE Orchestrator_Agent SHALL move the Content_Output to `./output/review/YYYY-MM-DD-[slug]/` and record it as "held for manual review" in the Agent_Log.
7. THE Orchestrator_Agent SHALL NOT upload any Content_Output to S3 until it has been explicitly approved via the Publish_Gate.
8. IF the run is unattended or automated, THE Orchestrator_Agent SHALL NOT auto-approve any Content_Output. All outputs SHALL be saved locally only and the user SHALL be notified via REQ-032 that manual Publish_Gate review is required.

### Requirement 31: Embargoed Content Release Check

**User Story:** As a content creator, I want to be notified when a previously embargoed article's release date has arrived, so that I can review and publish it at the right time.

#### Acceptance Criteria

1. WHEN a run begins, THE Orchestrator_Agent SHALL query AgentCore Memory (long-term) for any Held_Items whose release date is on or before the current run date.
2. WHEN one or more Held_Items are found whose release date has passed, THE Orchestrator_Agent SHALL notify the user at the start of the run, listing each Held_Item with its filename, original run date, release date, and local file path.
3. THE Orchestrator_Agent SHALL ask the user whether to include each released Held_Item in the current run's Publish_Gate review.
4. WHEN the user confirms a Held_Item for inclusion, THE Orchestrator_Agent SHALL add it to the current Publish_Gate review queue alongside any newly generated Content_Outputs.
5. WHEN a Held_Item passes the Publish_Gate and is approved, THE Orchestrator_Agent SHALL remove it from AgentCore Memory (long-term) and include it in the S3 upload.
6. THE Orchestrator_Agent SHALL record all embargo release checks in the Agent_Log.

### Requirement 32: Release Notification

**User Story:** As a content creator, I want to receive a notification when embargoed content is ready for review, so that I don't miss a release window.

#### Acceptance Criteria

1. WHEN a Held_Item's release date arrives and a scheduled run detects it (per REQ-031), THE Orchestrator_Agent SHALL send a release notification to the user containing:
   - Subject: "Magic Content Engine — embargo lifted: [title]"
   - The filename and local file path of the held content
   - The original embargo release date
   - A reminder to review before publishing
2. THE Orchestrator_Agent SHALL send notifications via Amazon SES to a configured recipient email address.
3. THE notification SHALL NOT publish or upload any content automatically. It is a reminder only.
4. IF SES delivery fails, THE Orchestrator_Agent SHALL log the failure in the Agent_Log and continue the run.
5. Auto-publish on embargo release is explicitly out of scope for v1. A human MUST review and approve all held content before it is uploaded to S3 or published externally.

> **Note for v2:** Consider human-in-the-loop confirmation via SES reply or Slack response to trigger S3 upload.

### Requirement 33: Topic Coverage Map

**User Story:** As a content creator, I want the engine to track which topics I have already covered, so that my content progressively builds on previous work rather than repeating the same ground.

#### Acceptance Criteria

1. THE Orchestrator_Agent SHALL maintain a Topic_Coverage_Map in AgentCore Memory (long-term) recording which topics have been covered, the article titles and run dates associated with each topic, and which adjacent topics have not yet been covered.
2. WHEN an Article is confirmed and content is generated, THE Orchestrator_Agent SHALL update the Topic_Coverage_Map in AgentCore Memory (long-term) with the topics covered in that run.
3. WHEN scoring Articles each run, THE Orchestrator_Agent SHALL use the Topic_Coverage_Map to identify topic gaps — topics relevant to the niche that have not yet been covered — and weight scoring toward Articles that fill those gaps.
4. THE Orchestrator_Agent SHALL identify a recommended focus topic for each run based on: topic gap analysis, current high-scoring articles, and natural topic progression (e.g. Runtime covered → Gateway is natural next).
5. THE Topic_Coverage_Map SHALL be included in the weekly brief presented to the user at the start of each run (REQ-035), showing covered topics, uncovered topics, and the recommended focus for this run.
6. THE Orchestrator_Agent SHALL record Topic_Coverage_Map updates in the Agent_Log.

### Requirement 34: Engagement Signal Tracking

**User Story:** As a content creator, I want the engine to track how my published content performs, so that I can concentrate future content on topics that resonate most with my audience.

#### Acceptance Criteria

1. WHEN a run begins, THE Orchestrator_Agent SHALL query the dev.to API to retrieve performance metrics for previously published posts, including: page views, reactions, comments, and reading time per post.
2. THE Orchestrator_Agent SHALL store engagement metrics per post in AgentCore Memory (long-term), keyed by post title and publication date.
3. WHEN scoring Articles each run, THE Orchestrator_Agent SHALL use engagement metrics to weight scoring toward topics that have previously performed well with the audience.
4. THE Orchestrator_Agent SHALL identify the top performing post from the past 7 days and include it in the weekly brief (REQ-035).
5. THE dev.to API credential SHALL be managed by AgentCore Identity as an outbound auth API key. In local development, it MAY be read from a `.env` file excluded from version control.
6. IF the dev.to API is unreachable or returns an error, THE Orchestrator_Agent SHALL log the failure in the Agent_Log, skip engagement tracking for that run, and continue with the standard scoring pipeline.
7. THE Orchestrator_Agent SHALL record engagement metric fetches and any failures in the Agent_Log.
8. WHEN the dev.to API returns no published posts for the configured account, THE Orchestrator_Agent SHALL treat this as a clean state, skip engagement tracking for that run, and record "no published content yet" in the Agent_Log. This is the expected state for new accounts and SHALL NOT be treated as an error.
9. WHEN no Engagement_Metrics exist in AgentCore Memory (long-term) from previous runs, THE Weekly_Brief (REQ-035) SHALL omit the top performing content section entirely and display a message such as: "No published content yet — engagement tracking will begin after your first post."
10. THE Orchestrator_Agent SHALL NOT weight scoring by engagement signals until at least one post has accumulated metrics. Until then, scoring SHALL rely solely on relevance criteria (REQ-005) and topic gap analysis (REQ-033).

### Requirement 35: Weekly Brief

**User Story:** As a content creator, I want a personalised weekly brief at the start of each run, so that I have clear context on what performed well, what I haven't covered yet, and what I should focus on this week before I see the article list.

#### Acceptance Criteria

1. WHEN a run begins and AFTER the embargo release check (REQ-031) and BEFORE the research crawl, THE Orchestrator_Agent SHALL generate and present a Weekly_Brief to the user using Claude Haiku.
2. THE Weekly_Brief SHALL contain the following sections:
   - Top performing content: the highest-engagement post from the past 7 days (title, views, reactions) from Engagement_Metrics (REQ-034). If no data is available, this section is omitted.
   - Topic coverage map: covered topics listed with most recent run date, uncovered topics in the niche listed as gaps.
   - Recommended focus: one recommended topic for this run, derived from topic gap analysis weighted by current engagement signals and available high-scoring articles.
3. THE Weekly_Brief SHALL be presented as a clean, readable terminal output before the research crawl begins.
4. THE user SHALL be able to override the recommended focus topic by entering a different topic or pressing Enter to accept the recommendation before the crawl begins.
5. WHEN the user overrides the recommended focus topic, THE Orchestrator_Agent SHALL use the override to weight scoring during the research phase.
6. THE Weekly_Brief content SHALL be included in the Agent_Log for the current run.
7. THE Weekly_Brief SHALL be generated using Claude Haiku to maintain cost efficiency.

## Out of Scope — v2 Considerations

### Multi-user profile support
The system is designed for a single user in v1. A second user 
(A second user from an AWS Partner organisation) has been identified for v2. 
v2 will introduce a profiles/ directory allowing per-user niche 
scoring, voice rules, research sources, and NDA configurations. 
Shared infrastructure (AgentCore Runtime, S3, SES) will serve 
both profiles. No v1 changes required — the current architecture 
supports this extension cleanly.

### Voice and avatar video generation
A collaborator suggested extending the pipeline to include automated voice and avatar video generation as a v3 capability.

The Magic Content Engine already produces script.md with clearly 
delineated scripted sections and <!-- MIKE: --> personal placeholders. 
This structure maps directly to a hybrid video production approach:

- Scripted sections → ElevenLabs (voice) + HeyGen or Synthesia 
  (avatar) → auto-generated video segments
- <!-- MIKE: --> cold open and outro → recorded personally → 
  spliced in during editing

Services required:
- ElevenLabs for voice cloning (already supported by AgentCore 
  Identity as an outbound auth provider)
- HeyGen or Synthesia for avatar video rendering
- A sixth output type: auto-generated video segments 
  (scripted portions only)

Note: personal sections (cold open, outro, Oceania angle) should 
always be recorded by the content owner. AI avatar video is 
detectable and community audiences value authenticity. The hybrid 
approach preserves credibility while automating the bulk of 
production.

No v1 or v2 changes required. The scripted/personal section 
structure in script.md already supports this extension.