# Requirements Document

## Introduction

The Bullpen Web GUI is a local web application that wraps the existing Magic Content Engine bullpen pipeline in a browser-based interface. It runs on localhost, requires no cloud hosting, and covers the full content workflow: topic selection, pipeline execution, live progress monitoring, content review and editing, and one-click publishing. The GUI replaces the terminal prompt approval gate and the manual file-navigation review workflow with a single browser tab.

The pipeline itself (`run_pipeline()` in `magic_content_engine/bullpen/editor_in_chief.py`) is already built and running. This feature adds a web layer on top of it — it does not change the pipeline logic.

## Glossary

- **Web_Server**: The local Flask or FastAPI application that serves the GUI and exposes the API endpoints. Runs on `localhost:5000` (or configurable port). No cloud hosting.
- **GUI**: The browser-based interface served by the Web_Server. Runs in a standard desktop browser.
- **Ideas_Panel**: The section of the GUI that surfaces topic suggestions from the DynamoDB topic coverage map and allows the user to enter a custom topic.
- **Pipeline_Runner**: The Web_Server component that invokes `run_pipeline()` from `editor_in_chief.py` in a background thread or process, so the GUI remains responsive during a run.
- **Progress_View**: The section of the GUI that displays live pipeline status by tailing `output/agent-log.jsonl` and rendering agent events in real time.
- **Review_Panel**: The section of the GUI that renders the generated `post.md` as HTML and exposes MIKE placeholder sections for inline editing.
- **Publish_Panel**: The section of the GUI that provides one-click publish actions: publish to dev.to, save as draft to dev.to, copy LinkedIn post text, and download YouTube script.
- **MIKE_Placeholder**: A comment in generated Markdown of the form `<!-- MIKE: [brief instruction, ~N words] -->`. These are the sections the user fills in before publishing.
- **AMILogEvent**: A structured JSON Lines event written to `output/agent-log.jsonl` during a pipeline run. Fields: `event_type`, `timestamp`, `agent_type`, `run_id`, `details`.
- **Topic_Coverage_Map**: The DynamoDB table `mce-topic-coverage` that records which topics have been covered and when. Used to generate topic suggestions.
- **Run_Bundle**: The output directory for a single pipeline run, e.g. `output/2026-05-10-weekly-update/`. Contains `post.md`, `script.md`, `cfp-proposal.md`, `usergroup-session.md`, `digest-email.txt`, `agent-log.jsonl`, and `checkpoints.json`.
- **DEVTO_API_KEY**: The dev.to API key stored in the environment variable `DEVTO_API_KEY`, loaded from `.env` via `magic_content_engine/config.py`.
- **Approval_Gate**: The point in the pipeline where the user reviews content and decides whether to publish. In the GUI this replaces the terminal `input()` prompt in `run_local.py`.

## Requirements

### Requirement 1: Local Web Server

**User Story:** As a content creator, I want a local web server I can start with a single command, so that the GUI is available in my browser without any cloud setup.

#### Acceptance Criteria

1. THE Web_Server SHALL start with a single command: `python scripts/run_gui.py` (or equivalent entry point), and the requirement is satisfied only if the server actually starts successfully and is ready to accept connections.
2. WHEN the Web_Server starts, THE Web_Server SHALL open the GUI at `http://localhost:5000` (default port, configurable via `GUI_PORT` environment variable).
3. THE Web_Server SHALL serve all GUI assets (HTML, CSS, JavaScript) from a local directory — no CDN dependencies required for core functionality.
4. IF the configured port is already in use, THEN THE Web_Server SHALL log a descriptive error and exit with a non-zero code.
5. THE Web_Server SHALL load environment variables from `.env` using the same mechanism as `magic_content_engine/config.py`.

### Requirement 2: Topic Suggestions (Ideas Panel)

**User Story:** As a content creator, I want to see a list of suggested topics based on what I have not covered recently, so that I can pick a direction without having to think from scratch.

#### Acceptance Criteria

1. WHEN the Ideas_Panel loads, THE Web_Server SHALL query the DynamoDB `mce-topic-coverage` table and return the topics not covered in the last 30 days, ordered by days-since-last-coverage descending.
2. THE Ideas_Panel SHALL display each suggestion with: the topic name, the date last covered (or "Never" if not previously covered), and a "Use this topic" button.
3. WHEN the user clicks "Use this topic", THE Ideas_Panel SHALL populate the topic input field with the selected topic.
4. IF the DynamoDB query fails, THEN THE Ideas_Panel SHALL display a non-blocking warning message and allow the user to enter a topic manually, regardless of whether the warning message itself displays successfully.
5. THE Ideas_Panel SHALL display a maximum of 10 suggestions at a time.
6. THE Ideas_Panel SHALL allow manual topic entry in all cases, including when the DynamoDB query succeeds.

### Requirement 3: Topic Input and Pipeline Trigger

**User Story:** As a content creator, I want to type a topic and hit Run to kick off the pipeline from the GUI, so that I do not need to use the terminal.

#### Acceptance Criteria

1. THE GUI SHALL provide a topic text input field and an output type selector (checkboxes for: blog, youtube, cfp, usergroup, digest).
2. WHEN the user submits the form with a non-empty topic and at least one output type selected, THE Web_Server SHALL invoke `run_pipeline()` in a background thread, passing a `BullpenBrief` constructed from the topic and selected output types. Clicking the Run button automatically submits the form.
3. THE GUI SHALL disable the Run button and topic input while a pipeline run is in progress, to prevent concurrent runs.
4. IF the topic field is empty when the user clicks Run, THEN THE GUI SHALL display a validation message and not invoke the pipeline.
5. IF no output types are selected when the user clicks Run, THEN THE GUI SHALL display a validation message and not invoke the pipeline.
6. WHEN a pipeline run starts, THE GUI SHALL navigate to or reveal the Progress_View automatically.

### Requirement 4: Live Pipeline Progress

**User Story:** As a content creator, I want to see live pipeline progress in the browser, so that I know which stage is running without watching terminal output.

#### Acceptance Criteria

1. WHILE a pipeline run is in progress, THE Progress_View SHALL poll or stream `output/agent-log.jsonl` and display new `AMILogEvent` entries as they are written, with a maximum latency of 2 seconds between file write and display update.
2. THE Progress_View SHALL display the current active agent using the `agent_type` field from the most recent `agent_invoked` event, highlighting it in the agent sequence: Researcher → Desk Editor → Writer → Subeditor → (Approval Gate) → Publisher.
3. WHEN an `agent_completed` event is received, THE Progress_View SHALL mark that agent as complete in the sequence display.
4. WHEN a `verdict` event is received, THE Progress_View SHALL display the filename, verdict (`publish` / `revise` / `spike`), and feedback text.
5. WHEN an `agent_error` event is received, THE Progress_View SHALL display the error message and mark the pipeline as halted. The pipeline SHALL continue processing any remaining steps while the error message remains visible.
6. WHEN the pipeline emits an `approval_gate_presented` event, THE Progress_View SHALL display the Approval Gate step as active and prompt the user to review content in the Review_Panel.
7. WHEN the pipeline run completes (status `success`, `halted`, or `error`), THE Progress_View SHALL display the final status and a summary of published and escalated files.

### Requirement 5: Content Review — Rendered Markdown

**User Story:** As a content creator, I want to read the generated post rendered as HTML in the browser, so that I can review it without opening a Markdown file.

#### Acceptance Criteria

1. WHEN a pipeline run produces a `post.md` file, THE Review_Panel SHALL render it as HTML using a standard Markdown library (e.g. `markdown-it-py`).
2. THE Review_Panel SHALL display a file selector allowing the user to switch between all output files in the current Run_Bundle: `post.md`, `script.md`, `cfp-proposal.md`, `usergroup-session.md`, `digest-email.txt` (showing only files that exist for the current run).
3. THE Review_Panel SHALL render standard Markdown elements: headings, paragraphs, bold, italic, code blocks, blockquotes, and unordered and ordered lists.
4. WHEN a file is selected in the Review_Panel, THE Web_Server SHALL read the file from the Run_Bundle directory and return its contents for rendering.
5. IF a requested file does not exist in the Run_Bundle, THEN THE Web_Server SHALL return a 404 response and THE Review_Panel SHALL display a "File not generated for this run" error message. An error message SHALL be displayed whenever a requested file does not exist, regardless of the HTTP response code.

### Requirement 6: Inline Editing of MIKE Placeholders

**User Story:** As a content creator, I want to click into the MIKE placeholder sections and write my hook and closing directly in the browser, so that the full editing workflow is in one place.

#### Acceptance Criteria

1. WHEN the Review_Panel renders a Markdown file, THE Review_Panel SHALL detect all `<!-- MIKE: [instruction, ~N words] -->` placeholders and render each as a visually distinct, clickable editable region displaying the instruction text.
2. WHEN the user clicks a MIKE placeholder region, THE Review_Panel SHALL replace it with an inline text editor (e.g. `contenteditable` div or lightweight editor) pre-populated with the instruction text as a placeholder hint.
3. WHEN the user saves their edits (via a Save button or keyboard shortcut), THE Web_Server SHALL write the updated content back to the source Markdown file in the Run_Bundle, replacing the `<!-- MIKE: ... -->` comment with the user's text.
4. THE Web_Server SHALL preserve all non-MIKE content in the file unchanged when saving edits.
5. WHEN a save completes successfully, THE Review_Panel SHALL display a confirmation message.
6. IF a save fails (e.g. file write error), THEN THE Review_Panel SHALL display an error message and retain the user's unsaved edits in the editor.

### Requirement 7: Approval Gate in the GUI

**User Story:** As a content creator, I want to approve or reject the pipeline output from the browser, so that I do not need to respond to a terminal prompt.

#### Acceptance Criteria

1. WHEN the pipeline reaches the approval gate (`approval_gate_presented` event), THE GUI SHALL display an Approve and a Reject button in the Review_Panel.
2. WHEN the user clicks Approve, THE Web_Server SHALL signal the waiting `approval_fn` to return `True`, allowing the pipeline to proceed to the Publisher.
3. WHEN the user clicks Reject, THE Web_Server SHALL signal the waiting `approval_fn` to return `False`, causing the pipeline to retain files without publishing.
4. THE GUI SHALL disable the Approve and Reject buttons once a decision has been made, to prevent double-submission.
5. WHEN the approval decision is recorded, THE Progress_View SHALL update to reflect the decision (approved or rejected) and the pipeline's subsequent state.

### Requirement 8: Publish to dev.to

**User Story:** As a content creator, I want a one-click Publish to dev.to button, so that I do not have to copy-paste Markdown into the dev.to editor.

#### Acceptance Criteria

1. WHEN the Review_Panel has a `post.md` file loaded, THE Publish_Panel SHALL display a "Publish to dev.to" button.
2. WHEN the user clicks "Publish to dev.to", THE Web_Server SHALL POST to the dev.to REST API endpoint `https://dev.to/api/articles` with the `post.md` content, using `DEVTO_API_KEY` from config, setting `published: true`.
3. WHEN the dev.to API returns a successful response (HTTP 201), THE Publish_Panel SHALL display the published article URL.
4. IF the dev.to API returns an error response, THEN THE Publish_Panel SHALL display the HTTP status code and error message returned by the API.
5. IF `DEVTO_API_KEY` is empty or not set, THEN THE Publish_Panel SHALL display the "Publish to dev.to" button in a disabled state alongside a configuration warning message.

### Requirement 9: Save as Draft to dev.to

**User Story:** As a content creator, I want a Save as draft button for dev.to, so that I can publish on my own schedule.

#### Acceptance Criteria

1. WHEN the Review_Panel has a `post.md` file loaded, THE Publish_Panel SHALL display a "Save as draft" button alongside the "Publish to dev.to" button.
2. WHEN the user clicks "Save as draft", THE Web_Server SHALL POST to `https://dev.to/api/articles` with the `post.md` content, using `DEVTO_API_KEY`, setting `published: false`.
3. WHEN the dev.to API returns a successful response (HTTP 201), THE Publish_Panel SHALL display the draft article URL. Only success information SHALL be displayed when the API call succeeds.
4. IF the dev.to API returns an error response, THEN THE Publish_Panel SHALL display the HTTP status code and error message returned by the API.

### Requirement 10: Copy LinkedIn Post Text

**User Story:** As a content creator, I want a Copy LinkedIn post button, so that I can share a summary without reformatting manually.

#### Acceptance Criteria

1. WHEN the current Run_Bundle contains a `digest-email.txt` file, THE Publish_Panel SHALL display a "Copy LinkedIn post" button.
2. WHEN the user clicks "Copy LinkedIn post", THE GUI SHALL copy the full text content of `digest-email.txt` to the system clipboard using the browser Clipboard API.
3. WHEN the copy succeeds, THE Publish_Panel SHALL display a "Copied!" confirmation that disappears after 3 seconds.
4. IF the browser Clipboard API is unavailable or the copy operation fails, THEN THE Publish_Panel SHALL display the text in a selectable textarea so the user can copy it manually.

### Requirement 11: Download YouTube Script

**User Story:** As a content creator, I want the YouTube script to be downloadable as a file, so that I have it ready when I film.

#### Acceptance Criteria

1. WHEN the current Run_Bundle contains a `script.md` file, THE Publish_Panel SHALL display a "Download YouTube script" button.
2. WHEN the user clicks "Download YouTube script", THE Web_Server SHALL serve `script.md` as a file download with the filename `script.md` and `Content-Disposition: attachment`.
3. THE download SHALL include the current saved state of `script.md`, including any edits made via the Review_Panel. IF another user or process is actively editing `script.md`, THE Web_Server SHALL wait for all active edits to be saved before serving the download, or block the download until editing is complete.

### Requirement 12: Run History

**User Story:** As a content creator, I want to see a list of previous pipeline runs, so that I can review or republish content from earlier runs without re-running the pipeline.

#### Acceptance Criteria

1. THE GUI SHALL display a run history list showing all Run_Bundle directories found under `output/`, ordered by directory name descending (most recent first).
2. WHEN the user selects a previous run from the history list, THE Review_Panel and Publish_Panel SHALL load the files from that run's directory.
3. THE run history list SHALL display for each run: the run directory name (e.g. `2026-05-10-weekly-update`) and the list of output files present in that directory.
4. WHEN a run is selected that has no `post.md`, THE Review_Panel SHALL display the first available output file for that run, or a "No reviewable files" message if no output files exist.

### Requirement 13: dev.to Article Metadata

**User Story:** As a content creator, I want to set the dev.to article title and tags before publishing, so that the article is correctly categorised on dev.to.

#### Acceptance Criteria

1. WHEN the user initiates a dev.to publish or draft action, THE Publish_Panel SHALL display editable fields for: article title (pre-populated from the first H1 heading in `post.md`) and tags (comma-separated, maximum 4 tags per dev.to API limits).
2. THE Web_Server SHALL include the title and tags fields in the dev.to API request body.
3. IF the title field is empty when the user submits, THEN THE Publish_Panel SHALL display a validation message and not submit the request. Submission SHALL be allowed as long as the title field itself is not empty, regardless of any other validation messages currently displayed.

### Requirement 14: API Error Handling and User Feedback

**User Story:** As a content creator, I want clear error messages when something goes wrong, so that I know what to fix without reading server logs.

#### Acceptance Criteria

1. IF the Web_Server encounters an unhandled exception during a pipeline run, THEN THE Web_Server SHALL log the full traceback to the server console and THE Progress_View SHALL display a human-readable error summary.
2. IF a dev.to API call fails due to a network error (connection refused, timeout), THEN THE Publish_Panel SHALL display "Could not reach dev.to — check your network connection" and not retry automatically.
3. IF the DynamoDB topic coverage query fails, THEN THE Ideas_Panel SHALL display "Could not load suggestions — check AWS credentials and region" and allow manual topic entry.
4. THE Web_Server SHALL return structured JSON error responses (with `error` and `detail` fields) for all API endpoints, so that the GUI can display consistent error messages.

### Requirement 15: No Concurrent Pipeline Runs

**User Story:** As a content creator, I want the GUI to prevent me from starting a second pipeline run while one is already running, so that output files are not corrupted by concurrent writes.

#### Acceptance Criteria

1. THE Web_Server SHALL maintain a run-in-progress flag that is set when a pipeline run starts and cleared when it completes or errors. IF a pipeline run starts but neither completes nor errors within a reasonable timeout period, THE Web_Server SHALL automatically clear the run-in-progress flag.
2. WHILE a pipeline run is in progress, THE Web_Server SHALL return HTTP 409 to any request to start a new run.
3. WHILE a pipeline run is in progress, THE GUI SHALL display the Run button as disabled with a "Pipeline running..." label.
4. WHEN the pipeline run completes or errors, THE GUI SHALL re-enable the Run button.

### Requirement 16: Output File Integrity on Save

**User Story:** As a content creator, I want my inline edits saved reliably, so that I do not lose work if the browser tab is closed.

#### Acceptance Criteria

1. WHEN the Web_Server writes edited content back to a Run_Bundle file, THE Web_Server SHALL write to a temporary file in the same directory first, validate that the temporary file is non-empty, and then atomically rename it to the target filename.
2. IF the atomic rename fails, THEN THE Web_Server SHALL return an error response and leave the original file unchanged.
3. THE Web_Server SHALL validate that the temporary file is non-empty before attempting the atomic rename, and SHALL confirm success to the GUI only after the rename completes successfully.
