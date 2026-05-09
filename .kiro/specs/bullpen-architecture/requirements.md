# Requirements Document

## Introduction

The Bullpen Architecture extends the Magic Content Engine from a single Orchestrator_Agent + Writing_Sub_Agent pattern to a constrained multi-agent system with six specialised agent types and an Editor-in-Chief orchestrator. Each agent type operates under the principle of least privilege: it receives only the inputs it needs, can only use explicitly allowed tools enforced via IAM execution roles, and can only write to designated outputs. The Editor-in-Chief accepts a weekly brief, invokes agents in sequence as a Lambda Durable Function using `step()` primitives, checkpoints progress automatically via the durable runtime, logs all decisions to DynamoDB, and pauses for Mike's approval using a `wait()` primitive before the Publisher runs. The Archivist (Whakaaro) runs on a nightly cadence as a separate Lambda, independent of the main content pipeline. The S3 bucket is `mce-second-brain` and the nightly context feed lives at `s3://mce-second-brain/ami-context/`.

## Glossary

- **Editor_in_Chief**: The top-level Lambda Durable Function that accepts a BullpenBrief, invokes constrained agent Lambdas in sequence via `step()`, manages the revision loop, gates the Publisher on Mike's approval via `wait()`, and logs all decisions to DynamoDB.
- **Researcher_Agent**: A Lambda function with read-only IAM permissions that crawls web sources and reads from S3 (`ami-context/`), producing a structured ResearchBrief. Its IAM role has no S3 PutObject and no SES permissions.
- **Desk_Editor_Agent**: A Lambda function that reads a ResearchBrief and produces a ContentBrief. Its IAM role has no S3 access and no SES permissions.
- **Writer_Agent**: A Lambda function that reads a ContentBrief and writes content files to S3 `output/` only. Its IAM role has no SES permissions and no S3 access outside the `output/` prefix.
- **Subeditor_Agent**: A Lambda function with read-only IAM permissions that reviews content and returns a verdict of `publish`, `revise`, or `spike`. Its IAM role has no write permissions of any kind.
- **Publisher_Agent**: A Lambda function that writes approved content to S3 `output/` and sends email via SES. Its IAM role cannot delete S3 objects and cannot write local files. It runs only after Mike's explicit approval.
- **Archivist_Agent**: The Whakaaro Lambda function that runs on a nightly-only cadence, reading from S3 `ami-context/` and writing summaries to S3 `archive/`. It is separate from the main content pipeline.
- **BullpenBrief**: A topic plus requested outputs document that the Editor_in_Chief receives to initiate a content run. Named `BullpenBrief` in code to avoid collision with the existing `WeeklyBrief` dataclass in `magic_content_engine/models.py`.
- **ResearchBrief**: The structured output of the Researcher_Agent, containing scored articles, source metadata, and relevance assessments.
- **ContentBrief**: The structured output of the Desk_Editor_Agent, containing article selection, angle, tone guidance, and output specifications for the Writer_Agent.
- **Verdict**: The Subeditor_Agent's assessment of a content piece: `publish` (ready for publication), `revise` (return to Writer_Agent with feedback), or `spike` (discard).
- **IAM_Execution_Role**: A per-Lambda IAM role that enforces tool allowlists at the infrastructure level. An agent Lambda cannot call an AWS service its role does not permit, regardless of what the LLM requests.
- **Tool_Allowlist**: The explicit set of AWS actions an agent Lambda is permitted to invoke, enforced by its IAM execution role.
- **Input_Output_Contract**: The formal definition of what data an agent Lambda receives as input and what it returns as output.
- **Checkpoint**: A progress record written automatically by the Lambda Durable Functions runtime after each `step()` completes, enabling resumption on failure.
- **AMI_Log**: The structured decision log written to DynamoDB `mce-run-history`, recording every agent invocation, output hash, verdict, and timing as JSON Lines.
- **Nightly_Context_Feed**: The S3 path `s3://mce-second-brain/ami-context/` containing approved Obsidian vault notes synced nightly from Mike's PC via Windows Task Scheduler.

## Requirements

### Requirement 1: Editor-in-Chief Weekly Brief Acceptance

**User Story:** As a content creator, I want the Editor-in-Chief to accept a weekly brief specifying a topic and requested outputs, so that each content run is focused and intentional.

#### Acceptance Criteria

1. WHEN a BullpenBrief is submitted, THE Editor_in_Chief SHALL validate that the brief contains a non-empty topic string and at least one requested output type from the set: blog post, YouTube script, CFP proposal, user group session outline, weekly digest email.
2. IF a BullpenBrief is missing a topic or contains zero requested outputs, THEN THE Editor_in_Chief SHALL reject the brief and return a descriptive validation error.
3. WHEN a valid BullpenBrief is accepted, THE Editor_in_Chief SHALL record the brief contents in the AMI_Log.

### Requirement 2: Agent Invocation via Lambda Durable Functions

**User Story:** As a developer, I want each agent invoked as a Lambda Durable Function step with IAM-enforced tool constraints, so that agent constraints are enforced at the infrastructure level.

#### Acceptance Criteria

1. WHEN the Editor_in_Chief invokes an agent, THE Editor_in_Chief SHALL use the `step()` primitive of the Lambda Durable Functions SDK, passing the agent Lambda name and input payload.
2. THE Editor_in_Chief SHALL invoke agents in the fixed sequence: Researcher_Agent, Desk_Editor_Agent, Writer_Agent, Subeditor_Agent, Publisher_Agent.
3. WHEN invoking an agent, THE Editor_in_Chief SHALL pass only the output of the preceding agent as input (no access to outputs from earlier pipeline stages beyond the immediate predecessor).
4. IF an agent invocation fails, THEN THE Editor_in_Chief SHALL log the failure in the AMI_Log and halt the pipeline with a descriptive error.

### Requirement 3: Researcher Agent Constraints

**User Story:** As a developer, I want the Researcher Agent to have read-only access to web sources and S3, so that it cannot accidentally modify any data or send messages.

#### Acceptance Criteria

1. THE Researcher_Agent System_Prompt SHALL define the role as "research analyst", specify that the agent can read web pages and read from S3 path `s3://mce-second-brain/ami-context/`, and state that writing to any location and sending messages are prohibited.
2. THE Researcher_Agent IAM_Execution_Role SHALL include only: S3 GetObject (scoped to the `mce-second-brain/ami-context/` prefix), Bedrock InvokeModel (Claude Haiku only), CloudWatch Logs.
3. THE Researcher_Agent IAM_Execution_Role SHALL exclude: S3 PutObject, S3 DeleteObject, SES SendEmail, and any action not explicitly listed.
4. WHEN the Researcher_Agent completes, THE Researcher_Agent SHALL return a ResearchBrief containing: a list of scored articles (title, URL, source, relevance score 1-5, one-sentence summary), the sources crawled, and any sources that failed.

### Requirement 4: Researcher Agent Input/Output Contract

**User Story:** As a developer, I want a formal contract for what the Researcher receives and returns, so that the pipeline is predictable and testable.

#### Acceptance Criteria

1. THE Researcher_Agent SHALL receive as input: the topic from the BullpenBrief, the list of primary and secondary Research_Sources from `.kiro/steering/02-research-sources.md` (parsed and passed by the Editor_in_Chief), and the nightly context feed path `s3://mce-second-brain/ami-context/`.
2. THE Researcher_Agent SHALL return as output: a JSON-serialisable ResearchBrief containing fields `articles` (list of scored article objects), `sources_crawled` (list of URLs attempted), `sources_failed` (list of URLs that failed after retries), and `run_timestamp` (ISO 8601).
3. FOR ALL valid ResearchBriefs, serialising the brief to JSON and deserialising it back SHALL produce an object equivalent to the original (round-trip property).

### Requirement 5: Desk Editor Agent Constraints

**User Story:** As a developer, I want the Desk Editor to work only from the research brief without web access, so that editorial decisions are based on curated research, not ad-hoc browsing.

#### Acceptance Criteria

1. THE Desk_Editor_Agent System_Prompt SHALL define the role as "desk editor", specify that the agent reads a ResearchBrief and produces a ContentBrief, and state that web access and sending messages are prohibited.
2. THE Desk_Editor_Agent IAM_Execution_Role SHALL include only: Bedrock InvokeModel (Claude Sonnet only), CloudWatch Logs.
3. THE Desk_Editor_Agent IAM_Execution_Role SHALL exclude: S3 operations, SES SendEmail, and any action not explicitly listed.
4. WHEN the Desk_Editor_Agent completes, THE Desk_Editor_Agent SHALL return a ContentBrief containing: selected articles (subset of ResearchBrief articles), the editorial angle, tone guidance referencing Voice_Rules from `.kiro/steering/01-niche-and-voice.md`, and the list of output types to generate.

### Requirement 6: Desk Editor Agent Input/Output Contract

**User Story:** As a developer, I want a formal contract for what the Desk Editor receives and returns, so that the handoff from research to writing is well-defined.

#### Acceptance Criteria

1. THE Desk_Editor_Agent SHALL receive as input: the ResearchBrief from the Researcher_Agent and the BullpenBrief topic.
2. THE Desk_Editor_Agent SHALL return as output: a JSON-serialisable ContentBrief containing fields `selected_articles` (list of article objects chosen for content), `editorial_angle` (string), `tone_guidance` (string referencing voice rules), `output_types` (list of requested output type strings), and `run_timestamp` (ISO 8601).
3. FOR ALL valid ContentBriefs, serialising the brief to JSON and deserialising it back SHALL produce an object equivalent to the original (round-trip property).

### Requirement 7: Writer Agent Constraints

**User Story:** As a developer, I want the Writer Agent to write only to the S3 output prefix, so that it cannot modify source code, scripts, or configuration.

#### Acceptance Criteria

1. THE Writer_Agent System_Prompt SHALL define the role as "content writer", specify that the agent reads a ContentBrief and writes content files, and state that web access, sending messages, and writing outside the `output/` S3 prefix are prohibited.
2. THE Writer_Agent IAM_Execution_Role SHALL include only: S3 PutObject (scoped to `mce-second-brain/output/` prefix), Bedrock InvokeModel (Claude Sonnet and Claude Haiku), CloudWatch Logs.
3. THE Writer_Agent IAM_Execution_Role SHALL exclude: S3 DeleteObject, SES SendEmail, and any action not explicitly listed.
4. THE Writer_Agent SHALL apply the Voice_Rules from `.kiro/steering/01-niche-and-voice.md` to all generated content, including: no banned phrases, no em-dashes, no opening paragraphs with "I", short sentences, and proper Placeholder format.
5. WHEN the Writer_Agent completes, THE Writer_Agent SHALL return a manifest listing all files written, with each file's path and word count.

### Requirement 8: Writer Agent Input/Output Contract

**User Story:** As a developer, I want a formal contract for what the Writer receives and returns, so that content generation is traceable.

#### Acceptance Criteria

1. THE Writer_Agent SHALL receive as input: the ContentBrief from the Desk_Editor_Agent, the steering file base path, the output directory path, and optionally revision feedback from the Subeditor_Agent (present only during revision re-invocations).
2. THE Writer_Agent SHALL return as output: a JSON-serialisable manifest containing fields `files_written` (list of objects with `path`, `output_type`, and `word_count`), `voice_rules_applied` (boolean, always true), and `run_timestamp` (ISO 8601).
3. THE Writer_Agent SHALL write content files following the existing output bundle structure: `post.md`, `script.md`, `description.txt`, `cfp-proposal.md`, `usergroup-session.md`, or `digest-email.txt` as appropriate for the requested output types.

### Requirement 9: Subeditor Agent Constraints

**User Story:** As a developer, I want the Subeditor to be strictly read-only, so that it reviews content without any ability to modify files or trigger side effects.

#### Acceptance Criteria

1. THE Subeditor_Agent System_Prompt SHALL define the role as "subeditor and quality reviewer", specify that the agent reads content files and returns a Verdict, and state that writing, web access, and sending messages are prohibited.
2. THE Subeditor_Agent IAM_Execution_Role SHALL include only: S3 GetObject (scoped to `mce-second-brain/output/` prefix), Bedrock InvokeModel (Claude Sonnet only), CloudWatch Logs.
3. THE Subeditor_Agent IAM_Execution_Role SHALL exclude: S3 PutObject, S3 DeleteObject, SES SendEmail, and any action not explicitly listed.
4. WHEN the Subeditor_Agent completes, THE Subeditor_Agent SHALL return a Verdict for each content file: one of `publish`, `revise`, or `spike`.
5. WHEN the Subeditor_Agent returns a `revise` Verdict, THE Subeditor_Agent SHALL include specific feedback describing what needs to change.
6. WHEN the Subeditor_Agent returns a `spike` Verdict, THE Subeditor_Agent SHALL include a rationale explaining why the content should be discarded.

### Requirement 10: Subeditor Agent Input/Output Contract

**User Story:** As a developer, I want a formal contract for what the Subeditor receives and returns, so that the review step is structured and auditable.

#### Acceptance Criteria

1. THE Subeditor_Agent SHALL receive as input: the Writer_Agent manifest (list of files written) and the S3 output prefix.
2. THE Subeditor_Agent SHALL return as output: a JSON-serialisable review containing fields `verdicts` (list of objects with `filename`, `verdict` as one of `publish`/`revise`/`spike`, and `feedback` string), and `run_timestamp` (ISO 8601).
3. THE Subeditor_Agent SHALL evaluate each content file against the Voice_Rules in `.kiro/steering/01-niche-and-voice.md` and the relevant output template steering file.

### Requirement 11: Revision Loop

**User Story:** As a developer, I want the Editor-in-Chief to loop content back to the Writer when the Subeditor says "revise", so that content quality improves before publication.

#### Acceptance Criteria

1. WHEN the Subeditor_Agent returns a `revise` Verdict for a content file, THE Editor_in_Chief SHALL re-invoke the Writer_Agent via `step()` with the original ContentBrief plus the Subeditor_Agent's feedback for that file.
2. THE Editor_in_Chief SHALL allow a maximum of two revision cycles per content file before escalating to Mike for manual review.
3. WHEN the maximum revision count is reached without a `publish` Verdict, THE Editor_in_Chief SHALL log the escalation in the AMI_Log and mark the file for manual review.
4. WHEN the Subeditor_Agent returns a `spike` Verdict, THE Editor_in_Chief SHALL discard the content file, log the spike rationale in the AMI_Log, and not re-invoke the Writer_Agent for that file.

### Requirement 12: Publisher Agent Constraints

**User Story:** As a developer, I want the Publisher to write only to S3 output and send email, with no ability to touch scripts or config, so that publication is safe and scoped.

#### Acceptance Criteria

1. THE Publisher_Agent System_Prompt SHALL define the role as "publisher", specify that the agent uploads approved content to S3 and sends notification email, and state that modifying scripts, configuration files, or any path outside the `output/` S3 prefix is prohibited.
2. THE Publisher_Agent IAM_Execution_Role SHALL include only: S3 PutObject (scoped to `mce-second-brain/output/` prefix), SES SendEmail, CloudWatch Logs.
3. THE Publisher_Agent IAM_Execution_Role SHALL exclude: S3 DeleteObject, S3 GetObject outside `output/`, and any action not explicitly listed.
4. THE Publisher_Agent SHALL upload only files that received a `publish` Verdict from the Subeditor_Agent.

### Requirement 13: Publisher Agent Input/Output Contract

**User Story:** As a developer, I want a formal contract for what the Publisher receives and returns, so that publication actions are fully auditable.

#### Acceptance Criteria

1. THE Publisher_Agent SHALL receive as input: the list of files with `publish` Verdicts, the S3 bucket name (`mce-second-brain`), and the S3 key prefix (`output/YYYY-MM-DD-[slug]/`).
2. THE Publisher_Agent SHALL return as output: a JSON-serialisable publication report containing fields `files_uploaded` (list of objects with `local_path` and `s3_key`), `email_sent` (boolean), `email_recipient` (string), and `run_timestamp` (ISO 8601).
3. IF an S3 upload fails, THEN THE Publisher_Agent SHALL retry up to three times with exponential backoff before logging the failure and continuing with remaining files.

### Requirement 14: Mike's Approval Gate Before Publisher

**User Story:** As a content creator, I want the pipeline to pause for my explicit approval before the Publisher runs, so that nothing gets published without my sign-off.

#### Acceptance Criteria

1. WHEN all content files have received their final Verdicts (after any revision loops), THE Editor_in_Chief SHALL send Mike an SES email summarising files and their Verdicts (filename, word count, first 3 lines per file) with signed approve/reject URLs, then pause execution using the `wait()` primitive.
2. WHEN Mike approves via the approve URL, THE Editor_in_Chief SHALL resume and invoke the Publisher_Agent with only the approved files.
3. WHEN Mike rejects via the reject URL, THE Editor_in_Chief SHALL log the rejection in the AMI_Log, skip the Publisher_Agent, and retain all files in S3 `output/`.
4. THE Editor_in_Chief SHALL record Mike's approval or rejection decision and timestamp in the AMI_Log.

### Requirement 15: Checkpoint Progress via Lambda Durable Functions

**User Story:** As a developer, I want each agent completion checkpointed, so that the pipeline can resume from the last successful step on failure.

#### Acceptance Criteria

1. WHEN an agent completes successfully, THE Lambda Durable Functions runtime SHALL automatically checkpoint the `step()` result, enabling resumption from that point on failure.
2. WHEN the Editor_in_Chief is re-invoked after a failure, THE Lambda Durable Functions runtime SHALL replay completed steps from checkpoints and resume from the first incomplete step.
3. THE Editor_in_Chief SHALL additionally write a Checkpoint record to DynamoDB `mce-checkpoints` after each agent completes, containing: agent type, completion timestamp, output hash, and status.
4. FOR ALL valid Checkpoint records, serialising to JSON and deserialising back SHALL produce an equivalent object (round-trip property).

### Requirement 16: Decision Logging to DynamoDB

**User Story:** As a developer, I want every agent invocation and decision logged in a structured format, so that the full pipeline is auditable.

#### Acceptance Criteria

1. THE Editor_in_Chief SHALL log the following events to DynamoDB `mce-run-history` as JSON Lines: agent invocation events (agent type, timestamp, input hash), agent completion events (agent type, timestamp, output hash, duration), Verdicts (filename, verdict, feedback), Mike's approval decisions, and errors.
2. WHEN any agent is invoked, THE Editor_in_Chief SHALL log the invocation event.
3. WHEN any agent completes, THE Editor_in_Chief SHALL log the completion event.
4. Each log event SHALL contain at minimum: `event_type` (string), `timestamp` (ISO 8601), `agent_type` (string), and `details` (object).
5. FOR ALL valid AMI_Log entries, serialising an entry to JSON and deserialising it back SHALL produce an equivalent object (round-trip property).

### Requirement 17: Archivist Agent (Whakaaro) Nightly Cadence

**User Story:** As a developer, I want the Archivist to run on a nightly schedule separate from the content pipeline, so that the knowledge archive stays current without blocking content production.

#### Acceptance Criteria

1. THE Archivist_Agent SHALL run on a nightly schedule via a separate EventBridge Scheduler rule, independent of the Editor_in_Chief content pipeline.
2. THE Archivist_Agent SHALL read from S3 path `s3://mce-second-brain/ami-context/` and write summaries to `s3://mce-second-brain/archive/`.
3. THE Editor_in_Chief SHALL NOT invoke the Archivist_Agent during a content run.

### Requirement 18: Archivist Agent Constraints

**User Story:** As a developer, I want the Archivist scoped to reading and archiving context data, so that it cannot interfere with the content pipeline or publish anything.

#### Acceptance Criteria

1. THE Archivist_Agent System_Prompt SHALL define the role as "knowledge archivist (Whakaaro)", specify that the agent reads from the nightly context feed and writes summaries to the archive, and state that publishing content, sending email, and modifying scripts or configuration are prohibited.
2. THE Archivist_Agent IAM_Execution_Role SHALL include only: S3 GetObject (scoped to `mce-second-brain/ami-context/` prefix), S3 PutObject (scoped to `mce-second-brain/archive/` prefix), CloudWatch Logs.
3. THE Archivist_Agent IAM_Execution_Role SHALL exclude: SES SendEmail, Bedrock InvokeModel outside approved models, and any action not explicitly listed.

### Requirement 19: System Prompt Structure

**User Story:** As a developer, I want a consistent structure for all agent system prompts, so that constraints are clear and enforceable.

#### Acceptance Criteria

1. THE Editor_in_Chief SHALL define each agent's System_Prompt with the following sections: Role (one-sentence role definition), Allowed Actions (explicit list of permitted operations), Hard Constraints (explicit list of prohibited operations), and Input/Output Format (description of expected input and required output structure).
2. THE System_Prompt for each agent SHALL state prohibited actions using the phrase "You MUST NOT" followed by the specific action.
3. THE System_Prompt for each agent SHALL state the IAM-enforced Tool_Allowlist explicitly, listing each permitted AWS action by name.

### Requirement 20: Tool Allowlist Enforcement via IAM

**User Story:** As a developer, I want tool allowlists enforced at the IAM level, so that an agent cannot call AWS services outside its permitted set even if its prompt is circumvented.

#### Acceptance Criteria

1. EACH agent Lambda SHALL be assigned a dedicated IAM execution role that permits only the AWS actions required for that agent's function.
2. IF an agent Lambda attempts to invoke an AWS action not permitted by its IAM role, THEN AWS SHALL reject the call with an AccessDenied error before the action executes.
3. THE Editor_in_Chief SHALL log any unexpected IAM AccessDenied errors in the AMI_Log.

### Requirement 21: Model Routing Per Agent

**User Story:** As a developer, I want each agent to use the appropriate model (Haiku or Sonnet) based on its task type, so that cost is optimised without sacrificing quality.

#### Acceptance Criteria

1. THE Researcher_Agent SHALL use Claude Haiku for relevance scoring and metadata extraction.
2. THE Desk_Editor_Agent SHALL use Claude Sonnet for editorial angle and tone decisions.
3. THE Writer_Agent SHALL use Claude Sonnet for narrative content (blog post, YouTube script, CFP proposal, user group session outline) and Claude Haiku for the weekly digest email.
4. THE Subeditor_Agent SHALL use Claude Sonnet for quality review and Verdict generation.
5. THE Publisher_Agent SHALL use Claude Haiku for email formatting.
6. THE Editor_in_Chief SHALL record the model used for each agent invocation in the AMI_Log.

### Requirement 22: Pipeline Sequence Integrity

**User Story:** As a developer, I want the agent pipeline to execute in a strict sequence with no out-of-order execution, so that each agent receives the correct predecessor's output.

#### Acceptance Criteria

1. THE Editor_in_Chief SHALL enforce the pipeline sequence via sequential `step()` calls: Researcher_Agent, then Desk_Editor_Agent, then Writer_Agent, then Subeditor_Agent, then (after Mike's approval via `wait()`) Publisher_Agent.
2. THE Lambda Durable Functions runtime SHALL NOT invoke the next `step()` until the current step completes and returns its output.
3. IF any agent in the sequence fails and cannot be retried, THEN THE Editor_in_Chief SHALL halt the pipeline, log the failure in the AMI_Log, and notify Mike.

### Requirement 23: Vault Sync via Windows Task Scheduler

**User Story:** As a developer, I want the Obsidian vault synced to S3 automatically every Sunday night NZT, so that Monday's pipeline always has fresh context with zero manual effort.

#### Acceptance Criteria

1. A PowerShell script `scripts/sync-vault-to-s3.ps1` SHALL sync the approved Obsidian vault to `s3://mce-second-brain/ami-context/` using `aws s3 sync`.
2. THE sync script SHALL run automatically via Windows Task Scheduler at Sunday 10pm NZT.
3. THE sync script SHALL be idempotent — running it multiple times SHALL produce the same S3 state.
4. IF the AWS CLI returns an error, THE sync script SHALL log the error and exit with a non-zero code.
5. No manual commands SHALL be required after initial Task Scheduler setup.

### Requirement 24: EventBridge Scheduler — Monday 9am NZT

**User Story:** As a developer, I want the pipeline triggered automatically every Monday morning NZT, so that content is ready at the start of the week.

#### Acceptance Criteria

1. AN EventBridge Scheduler rule SHALL trigger the Editor_in_Chief Lambda every Monday at 9am in the `Pacific/Auckland` timezone.
2. THE schedule expression SHALL be `cron(0 9 ? * MON *)` with timezone set to `Pacific/Auckland`.
3. A SEPARATE EventBridge Scheduler rule SHALL trigger the Archivist_Agent Lambda nightly at 11pm NZT.
4. BOTH rules SHALL be confirmed firing correctly in ap-southeast-2.

### Requirement 25: Graceful Error Handling Across Pipeline

**User Story:** As a developer, I want errors in one agent to be contained and logged without corrupting the pipeline state, so that failures are recoverable.

#### Acceptance Criteria

1. IF the Researcher_Agent fails, THEN THE Editor_in_Chief SHALL log the error, record a Checkpoint to DynamoDB, and halt the pipeline (no downstream agents can run without research).
2. IF the Desk_Editor_Agent fails, THEN THE Editor_in_Chief SHALL log the error, record a Checkpoint to DynamoDB, and halt the pipeline.
3. IF the Writer_Agent fails for a specific output type, THEN THE Editor_in_Chief SHALL log the error, skip that output type, and continue with remaining output types.
4. IF the Subeditor_Agent fails, THEN THE Editor_in_Chief SHALL log the error and mark all pending content files for manual review by Mike.
5. IF the Publisher_Agent fails for a specific file, THEN THE Publisher_Agent SHALL log the error and continue uploading remaining files.

### Requirement 26: Backward Compatibility with Existing Output Bundle

**User Story:** As a developer, I want the bullpen architecture to produce output bundles in the same structure as the existing system, so that downstream consumers are not broken.

#### Acceptance Criteria

1. THE Writer_Agent SHALL produce output files following the existing bundle structure defined in `.kiro/steering/06-model-routing-and-bundle.md`: `YYYY-MM-DD-[slug]/` containing `post.md`, `script.md`, `description.txt`, `cfp-proposal.md`, `usergroup-session.md`, `references.bib`, `cost-estimate.txt`, `screenshots/`, and `agent-log.jsonl`.
2. THE Publisher_Agent SHALL upload to S3 bucket `mce-second-brain` under the key prefix `output/YYYY-MM-DD-[slug]/`.
3. THE `output/` key prefix SHALL NOT change — it is hardcoded in the admin importer.
