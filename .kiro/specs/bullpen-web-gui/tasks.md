# Implementation Plan: Bullpen Web GUI

## Overview

Build a local Flask web application that wraps the existing `run_pipeline()` function in a browser-based interface. All new code lives under `scripts/gui/`. The pipeline package (`magic_content_engine/`) is not modified. The implementation proceeds in layers: project scaffold → Flask app skeleton → API endpoints → SSE log tailing → approval gate → frontend → publish actions → tests.

## Tasks

- [ ] 1. Scaffold project structure and entry point
  - Create `scripts/gui/__init__.py`, `scripts/gui/app.py`, `scripts/gui/pipeline_runner.py`, `scripts/gui/log_tailer.py`, `scripts/gui/devto_client.py`
  - Create `scripts/gui/static/` directory with placeholder `index.html`, `app.js`, `style.css`
  - Create `scripts/gui/static/vendor/` directory (for `marked.min.js`)
  - Create `scripts/run_gui.py` entry point that starts Flask on `localhost:5000` (configurable via `GUI_PORT`)
  - Add `flask>=3.0,<4` and `markdown-it-py>=3.0,<4` to `pyproject.toml`
  - _Requirements: 1.1, 1.2, 1.3, 1.5_

- [ ] 2. Implement core Flask app state and run management
  - [ ] 2.1 Define `RunState` dataclass and global `_run_state` / `_run_lock` in `app.py`
    - Fields: `in_progress`, `run_id`, `approval_event`, `approval_result`, `log_path`, `output_dir`
    - _Requirements: 15.1_
  - [ ] 2.2 Implement `POST /api/run` endpoint
    - Validate non-empty topic (422) and at least one output type
    - Return 409 if `run_in_progress` is True
    - Construct `BullpenBrief`, start background thread, return 202 with `run_id`
    - _Requirements: 3.2, 3.4, 3.5, 15.1, 15.2_
  - [ ]* 2.3 Write unit tests for `POST /api/run`
    - `test_run_endpoint_rejects_concurrent_runs` — POST twice, second returns 409
    - `test_run_endpoint_validates_empty_topic` — empty topic returns 422
    - _Requirements: 3.4, 15.2_

- [ ] 3. Implement pipeline background thread and approval gate
  - [ ] 3.1 Implement `_pipeline_thread` in `pipeline_runner.py`
    - Calls `run_pipeline()` with `BullpenBrief` and approval fn
    - Sets `run_state.in_progress = False` on completion or unhandled exception
    - Logs full traceback on exception; emits synthetic `pipeline_complete` event with `status=error`
    - _Requirements: 14.1, 15.1_
  - [ ] 3.2 Implement `_make_approval_fn` in `pipeline_runner.py`
    - Returns a callable that blocks on `approval_event.wait()` then returns `approval_result`
    - _Requirements: 7.2, 7.3_
  - [ ] 3.3 Implement `POST /api/run/approve` and `POST /api/run/reject` endpoints
    - Set `approval_result` and call `approval_event.set()`
    - Return 409 if no approval gate is currently waiting
    - _Requirements: 7.2, 7.3, 7.4_
  - [ ]* 3.4 Write unit tests for approval gate
    - `test_approval_gate_approve` — mock pipeline thread, POST approve, verify `approval_fn` returns True
    - `test_approval_gate_reject` — same for reject
    - _Requirements: 7.2, 7.3_

- [ ] 4. Implement SSE log tailing
  - [ ] 4.1 Implement `_tail_log` generator in `log_tailer.py`
    - Opens `output/agent-log.jsonl`, seeks to end, polls for new lines every 1 second
    - Yields each new JSON line as an SSE `data:` frame
    - Emits synthetic `pipeline_complete` event when pipeline thread exits
    - Closes cleanly when client disconnects
    - _Requirements: 4.1_
  - [ ] 4.2 Implement `GET /api/run/status` SSE endpoint in `app.py`
    - Returns `text/event-stream` response using `_tail_log`
    - _Requirements: 4.1, 4.6, 4.7_

- [ ] 5. Checkpoint — core pipeline API complete
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 6. Implement Run Bundle file API
  - [ ] 6.1 Implement `GET /api/runs` endpoint
    - Lists all `output/` subdirectories, sorted descending by name
    - Excludes `agent-log.jsonl` and `checkpoints.json` from each run's `files` list
    - _Requirements: 12.1, 12.3_
  - [ ] 6.2 Implement `GET /api/runs/<run_id>/file?name=<filename>` endpoint
    - Returns raw file content as `text/plain; charset=utf-8`
    - Returns 404 if file does not exist
    - _Requirements: 5.4, 5.5, 12.2_
  - [ ] 6.3 Implement `POST /api/runs/<run_id>/file` endpoint with atomic write
    - Writes to temp file in same directory, validates non-empty, atomically renames to target
    - Returns 500 with structured error if rename fails, leaving original unchanged
    - _Requirements: 6.3, 6.4, 16.1, 16.2, 16.3_
  - [ ] 6.4 Implement `GET /api/runs/<run_id>/download/<filename>` endpoint
    - Serves file with `Content-Disposition: attachment; filename=<filename>`
    - _Requirements: 11.2, 11.3_
  - [ ]* 6.5 Write unit tests for file API
    - `test_file_save_atomic` — POST file, verify temp+rename, verify original unchanged on error
    - _Requirements: 16.1, 16.2, 16.3_
  - [ ]* 6.6 Write property test: round-trip file save
    - **Property 1: Round-trip file save** — for any valid Markdown string, saving via the file save endpoint and reading it back produces the identical string
    - **Validates: Requirements 6.3, 16.1, 16.3**

- [ ] 7. Implement DynamoDB suggestions endpoint
  - [ ] 7.1 Implement `GET /api/suggestions` endpoint
    - Queries DynamoDB `mce-topic-coverage`, returns up to 10 topics not covered in last 30 days, ordered by days-since-last-coverage descending
    - On DynamoDB failure, returns 200 with `{"suggestions": [], "warning": "..."}`
    - _Requirements: 2.1, 2.4, 2.5_
  - [ ]* 7.2 Write unit test for suggestions endpoint
    - `test_suggestions_dynamodb_failure` — mock DynamoDB failure, verify graceful degradation with warning field
    - _Requirements: 2.4, 14.3_

- [ ] 8. Implement dev.to publish endpoint
  - [ ] 8.1 Implement `devto_client.py` with `publish_article` function
    - POSTs to `https://dev.to/api/articles` with `body_markdown`, `title`, `tags`, `published` flag
    - Uses `DEVTO_API_KEY` from config; returns structured error on non-201 or network failure
    - _Requirements: 8.2, 9.2_
  - [ ] 8.2 Implement `POST /api/publish/devto` endpoint in `app.py`
    - Reads `post.md` from run bundle, calls `devto_client.publish_article`
    - Returns 400 if `DEVTO_API_KEY` is empty
    - Returns dev.to response body on 201, structured error otherwise
    - _Requirements: 8.2, 8.3, 8.4, 8.5, 9.2, 9.3, 9.4, 13.2_
  - [ ]* 8.3 Write unit tests for dev.to publish
    - `test_devto_publish_missing_api_key` — empty `DEVTO_API_KEY`, verify 400 response
    - _Requirements: 8.5_

- [ ] 9. Checkpoint — all API endpoints complete
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 10. Build frontend HTML structure
  - [ ] 10.1 Write `index.html` with all panel sections
    - Ideas Panel (suggestions list, topic input, output type checkboxes, Run button)
    - Progress View (agent sequence: Researcher → Desk Editor → Writer → Subeditor → Approval Gate → Publisher; verdict/error area)
    - Review Panel (file selector, rendered content area, Approve/Reject buttons hidden by default)
    - Publish Panel (title field, tags field, Publish/Draft/Copy LinkedIn/Download buttons)
    - Run History sidebar or section
    - _Requirements: 2.2, 3.1, 4.2, 5.2, 7.1, 8.1, 9.1, 10.1, 11.1, 12.1, 13.1_
  - [ ] 10.2 Write `style.css` with panel layout and component styles
    - Visually distinct MIKE placeholder regions (`.mike-placeholder`)
    - Agent sequence with active/complete/pending states
    - Disabled button states
    - _Requirements: 6.1, 4.2_

- [ ] 11. Implement frontend JavaScript — pipeline control and SSE
  - [ ] 11.1 Implement topic form submission and Run button logic in `app.js`
    - Validate non-empty topic and at least one output type before submitting
    - POST to `/api/run`, disable Run button and inputs on success
    - Navigate to Progress View on run start
    - Re-enable Run button when `pipeline_complete` event received
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6, 15.3, 15.4_
  - [ ] 11.2 Implement `startSSE` and `handlePipelineEvent` in `app.js`
    - Handle `agent_invoked`, `agent_completed`, `agent_error`, `verdict`, `approval_gate_presented`, `pipeline_complete`
    - Update agent sequence display, show verdict details, show error messages
    - Show Approve/Reject buttons on `approval_gate_presented`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 7.1_
  - [ ] 11.3 Implement Approve/Reject button handlers in `app.js`
    - POST to `/api/run/approve` or `/api/run/reject`
    - Disable both buttons after first click
    - Update Progress View to reflect decision
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

- [ ] 12. Implement frontend JavaScript — suggestions and run history
  - [ ] 12.1 Implement suggestions loading in `app.js`
    - Fetch `/api/suggestions` on Ideas Panel load
    - Render each suggestion with topic name, last covered date (or "Never"), and "Use this topic" button
    - Populate topic input on "Use this topic" click
    - Show warning message on failure; keep manual entry available
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_
  - [ ] 12.2 Implement run history loading in `app.js`
    - Fetch `/api/runs` and render list with run name and file list
    - On run selection, load files into Review Panel and Publish Panel
    - Show first available file or "No reviewable files" if none exist
    - _Requirements: 12.1, 12.2, 12.3, 12.4_

- [ ] 13. Implement frontend JavaScript — Review Panel and MIKE placeholders
  - [ ] 13.1 Vendor `marked.min.js` into `static/vendor/` and wire up client-side Markdown rendering
    - Download `marked.js` 15.x and save as `scripts/gui/static/vendor/marked.min.js`
    - Render file content using `marked.parse()` in the Review Panel content area
    - _Requirements: 5.1, 5.3_
  - [ ] 13.2 Implement MIKE placeholder detection and editable region rendering in `app.js`
    - Detect `<!-- MIKE: [instruction, ~N words] -->` patterns in raw Markdown before rendering
    - Replace each with a `<div class="mike-placeholder" data-instruction="...">` element
    - On click, replace with `<textarea>` pre-populated with instruction as placeholder hint
    - On Save, POST updated content to `/api/runs/<run_id>/file` with placeholder replaced by user text
    - Show "Saved" confirmation on success; show error and retain edits on failure
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_
  - [ ]* 13.3 Write unit test for MIKE placeholder detection
    - `test_mike_placeholder_detection` — Python equivalent of placeholder regex, verify detection and replacement
    - _Requirements: 6.1_
  - [ ]* 13.4 Write property test: MIKE placeholder preservation
    - **Property 2: MIKE placeholder preservation** — for any Markdown string containing zero or more valid MIKE placeholders, replacing all placeholders with user text and saving produces a file where no `<!-- MIKE:` patterns remain
    - **Validates: Requirements 6.3, 6.4**

- [ ] 14. Implement frontend JavaScript — Publish Panel actions
  - [ ] 14.1 Implement dev.to publish and draft actions in `app.js`
    - Pre-populate title from first H1 in `post.md`; validate non-empty title before submit
    - POST to `/api/publish/devto` with `published: true` (Publish) or `published: false` (Draft)
    - Display article URL on 201; display status code and error message on failure
    - Disable "Publish to dev.to" and "Save as draft" buttons with config warning when `DEVTO_API_KEY` absent
    - _Requirements: 8.1, 8.3, 8.4, 8.5, 9.1, 9.3, 9.4, 13.1, 13.3_
  - [ ] 14.2 Implement "Copy LinkedIn post" action in `app.js`
    - Show button only when `digest-email.txt` exists in current run bundle
    - Copy full text to clipboard using Clipboard API; show "Copied!" for 3 seconds
    - Fall back to selectable textarea if Clipboard API unavailable
    - _Requirements: 10.1, 10.2, 10.3, 10.4_
  - [ ] 14.3 Implement "Download YouTube script" action in `app.js`
    - Show button only when `script.md` exists in current run bundle
    - Trigger download via `/api/runs/<run_id>/download/script.md`
    - _Requirements: 11.1, 11.2_

- [ ] 15. Checkpoint — full UI wired end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 16. Wire entry point and validate startup behaviour
  - [ ] 16.1 Complete `scripts/run_gui.py` entry point
    - Load `.env` via `magic_content_engine/config.py` mechanism
    - Read `GUI_PORT` from environment (default 5000)
    - Catch `OSError` for port-in-use, log descriptive error, exit with code 1
    - _Requirements: 1.1, 1.2, 1.4, 1.5_

- [ ] 17. Final checkpoint — all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- All tests live in `magic_content_engine/test_bullpen_web_gui.py` using `pytest` and Flask's test client
- DynamoDB and dev.to calls are mocked in tests — no AWS credentials required
- The pipeline package (`magic_content_engine/`) is not modified by any task
- Property tests use Hypothesis; unit tests use pytest with Flask test client
- `marked.min.js` is vendored locally — no CDN dependency for core functionality

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2.1", "4.1", "6.1", "7.1", "8.1", "10.1", "10.2"] },
    { "id": 2, "tasks": ["2.2", "3.1", "3.2", "4.2", "6.2", "6.3", "6.4", "8.2", "11.1", "12.1", "12.2", "13.1"] },
    { "id": 3, "tasks": ["2.3", "3.3", "6.5", "7.2", "8.3", "11.2", "11.3", "13.2", "14.1", "14.2", "14.3"] },
    { "id": 4, "tasks": ["3.4", "6.6", "13.3", "16.1"] },
    { "id": 5, "tasks": ["13.4"] }
  ]
}
```
