# Requirements Document

## Introduction

This document specifies the requirements for the **Bullpen Console (Go)** — a
rewrite of the local desktop control panel ("Bullpen Console") from Python/Flask
to Go. These requirements are **derived from the approved design document**
(`design.md`) and the locked decisions in `CONTEXT.md` and
[ADR-0001](../../../docs/adr/0001-go-console-spawns-python-bullpen.md). This is
the design-first workflow: the design (including its Correctness Properties
section) already exists, and the requirements below are numbered so the
forward property references in the design (`Validates: Requirements 1.1, 2.1,
3.1, 3.2, 4.1, 5.1, 6.1`) resolve to the criteria stated here.

The rewrite covers **only the Console** (the desktop shell). The Bullpen (the
Python newsroom of editorial agents under `magic_content_engine/bullpen/`) keeps
its editorial logic unchanged. The two layers integrate through a **shared run
directory** (`output/<run_id>/`): the Go Console spawns a headless Python runner
per Run, tails the `agent-log.jsonl` event stream the runner writes, and signals
the approval gate by writing an `approval-decision.json` control file. AWS stays
entirely on the Python side; the Console carries no AWS SDK and needs no AWS
credentials to boot. Scope is a single machine; packaging is out of scope. The
legacy Flask Console is kept alive in parallel until the Go Console proves
itself.

## Glossary

- **Bullpen_Console**: The Go desktop shell being built. Owns HTTP/UI, SSE
  streaming, file review/edit/download, dev.to publishing, suggestions, the
  system tray, port handling, and browser launch. Contains no editorial logic
  and no AWS SDK.
- **Bullpen**: The Python newsroom of editorial agents and the pipeline that
  orchestrates them, under `magic_content_engine/bullpen/`. Unchanged in
  editorial logic by this rewrite.
- **Run**: A single end-to-end execution of the Bullpen pipeline for one topic,
  producing an output bundle under `output/<run_id>/`, identified by a `run_id`.
- **HTTP_Server**: The `net/http` server and router inside the Bullpen_Console
  (`internal/server`) that exposes the API surface.
- **Run_Manager**: The Bullpen_Console component (`internal/run`) that owns the
  lifecycle of the single active Run, spawns the headless runner, enforces
  single-active-run, and writes approval decisions.
- **SSE_Hub**: The Bullpen_Console component (`internal/sse`) that tails
  `agent-log.jsonl` and streams events as Server-Sent Events with replay and
  deduplication.
- **File_Service**: The Bullpen_Console component (`internal/files`) that lists,
  reads, saves, and resolves downloads for run-bundle files with path-traversal
  protection.
- **Suggestion_Service**: The Bullpen_Console component (`internal/vault`) that
  produces vault-only topic suggestions (recency list and title search).
- **Devto_Publisher**: The Bullpen_Console component (`internal/devto`) that
  publishes a Run's `post.md` to dev.to over plain HTTPS.
- **Desktop**: The Bullpen_Console component (`internal/desktop`) providing the
  native system tray, port selection, and browser launch.
- **Headless_Runner**: The new Python entry point (`scripts/run_headless.py`)
  the Console spawns per Run. Silent except for `agent-log.jsonl`; uses the
  control-file approval function.
- **Wiring_Factory**: The new shared Python module
  (`magic_content_engine/bullpen/wiring.py`) exposing
  `build_agent_callables(...)`, the single place all agent callables are wired,
  parameterised by which `approval_fn` to use.
- **Approval_Gate**: The cross-process human sign-off point. The Console writes
  `approval-decision.json`; the Bullpen `approval_fn` polls, reads, and deletes
  it.
- **Editor_in_Chief**: The Bullpen agent that orchestrates the pipeline and owns
  the approval gate. Emits `approval_gate_presented` and `pipeline_complete`.
- **Verdict**: The Subeditor's judgement on one content file: `publish`,
  `revise`, or `spike`.
- **files_published**: The set of files that earned a `publish` verdict; the
  pipeline reaches the approval gate only when this set is non-empty.
- **DedupKey**: The composite identity `timestamp | event_type | agent_type`
  used to suppress duplicate rendering of replayed SSE events.
- **Legacy_Flask_Console**: The existing Python/Flask Console
  (`scripts/gui/app.py`, `run_gui.py`, `log_tailer.py`, `bullpen.bat`) retained
  in parallel during the transition.
- **Vault**: The Obsidian-style note store referenced by `VAULT_PATH`, with
  `06-permanent/` and `00-inbox/` folders.

## Requirements

### Requirement 1: SSE Log Streaming with Replay and Reconnect Deduplication

**User Story:** As Mike, I want the Console to stream a Run's agent log live and
survive browser refresh and EventSource reconnects without double-rendering
events, so that I can watch progress reliably without the duplicate-event bug
that affected the Flask Console.

#### Acceptance Criteria

1. WHEN a client connects or reconnects to the SSE stream and events are
   replayed from the log, THE SSE_Hub SHALL emit each distinct event, identified
   by the DedupKey `timestamp | event_type | agent_type`, exactly once per
   stream, emitting no duplicate and dropping no distinct event.
2. WHEN a client connects to `GET /api/run/status`, THE SSE_Hub SHALL replay
   events from file offset 0 of `agent-log.jsonl` so the client can rebuild the
   full timeline.
3. WHEN responding to `GET /api/run/status`, THE SSE_Hub SHALL set the response
   header `Content-Type` to `text/event-stream`, `Cache-Control` to `no-cache`,
   and `X-Accel-Buffering` to `no`.
4. WHEN emitting a log event, THE SSE_Hub SHALL write it as a single SSE frame
   of the form `data: <event json>`.
5. WHEN `GET /api/run/status` includes a `run_id` query parameter, THE SSE_Hub
   SHALL select the run directory `output/<run_id>/` as the tail source.
6. WHEN an event with an `event_type` outside the known set is received, THE
   SSE_Hub SHALL emit the raw event rather than dropping it.
7. WHILE the Run is no longer active and the log file is idle, THE SSE_Hub SHALL
   emit exactly one synthetic terminal frame
   (`event: pipeline_complete` / `data: {"status":"complete"}`) and then end the
   stream.
8. WHEN the client disconnects, THE SSE_Hub SHALL close the stream cleanly.
9. WHEN a stream begins for a run directory whose `agent-log.jsonl` does not yet
   exist, THE SSE_Hub SHALL create the empty log file before tailing so the
   stream does not error.

### Requirement 2: Cross-Process Approval Gate via Control File

**User Story:** As Mike, I want my Approve/Reject click in the Console to cross
the process boundary to the Python pipeline reliably, so that the human approval
gate works even though the Console and the Bullpen are separate processes.

#### Acceptance Criteria

1. WHEN the Bullpen_Console writes an `approval-decision.json` containing
   `decision` of `"approved"` or `"rejected"` and the Bullpen `approval_fn`
   subsequently reads it, THE Approval_Gate SHALL return `true` for `"approved"`
   and `false` for `"rejected"`, and SHALL delete `approval-decision.json` from
   the run directory before returning.
2. WHEN the Bullpen_Console records an approval decision, THE Run_Manager SHALL
   write `output/<run_id>/approval-decision.json` atomically by writing a
   `.tmp` sibling and renaming it into place, so the Bullpen poller never reads
   a partially written file.
3. THE Run_Manager SHALL populate `approval-decision.json` with `decision`
   (`"approved"` or `"rejected"`), `decided_at` as an ISO 8601 timestamp, and
   `run_id` equal to the active Run's id.
4. WHEN the Bullpen `approval_fn` reaches the gate, THE Approval_Gate SHALL
   delete any pre-existing `approval-decision.json` before polling, so a
   decision from an earlier gate is never honoured.
5. WHILE awaiting a decision, THE Approval_Gate SHALL poll for
   `approval-decision.json` at an interval of approximately 1 second.
6. IF the Approval_Gate reads an `approval-decision.json` that contains partial
   or invalid JSON, THEN THE Approval_Gate SHALL wait one poll interval and
   retry rather than terminating the pipeline.
7. IF the Approval_Gate reads a `decision` value other than `"approved"` or
   `"rejected"`, THEN THE Approval_Gate SHALL ignore the value and continue
   polling.

### Requirement 3: Terminal States

**User Story:** As Mike, I want the Console to visibly distinguish exactly how a
Run ended, so that I always know whether to approve, to review escalated files,
or to diagnose a failure.

#### Acceptance Criteria

1. WHEN `files_published` is non-empty at the approval point, THE
   Bullpen_Console SHALL present the Approval_Gate; and WHERE `files_published`
   is empty, THE Bullpen_Console SHALL proceed without presenting an
   Approval_Gate.
2. WHEN a Run ends, THE Bullpen_Console SHALL settle into exactly one of the
   terminal states {Gate-presented, Escalated, Errored} and SHALL render exactly
   one terminal frame.
3. WHEN the Bullpen_Console is in the Gate-presented state, THE Bullpen_Console
   SHALL display Approve and Reject controls and the list of files in
   `files_pending_approval`.
4. WHEN `pipeline_complete` is received with no publish verdict and one or more
   `file_escalated` events have been seen, THE Bullpen_Console SHALL display the
   Escalated state listing each escalated file and its reason.
5. IF an `agent_error` event halts the pipeline, or `pipeline_complete` reports
   a `status` of `error` or `halted`, or the runner subprocess exits with a
   nonzero code, THEN THE Bullpen_Console SHALL display the Errored state showing
   the failing step and message.
6. IF the runner subprocess exits with a nonzero code and no terminal
   `pipeline_complete` event was received, THEN THE Run_Manager SHALL synthesise
   an Errored terminal frame so the UI does not hang.
7. WHEN both a subprocess exit and a terminal `pipeline_complete` event occur,
   THE Run_Manager SHALL mark the Run terminal on whichever signal arrives first
   and reconcile the other.

### Requirement 4: File Review, Edit, and Download Safety

**User Story:** As Mike, I want to list, read, edit, and download the files a Run
produced through the Console, so that I can review and adjust the output bundle
safely without any request escaping the run directory.

#### Acceptance Criteria

1. WHEN any file endpoint resolves a path from a `run_id` and a `name`, THE
   File_Service SHALL confine the resolved path to `output/<run_id>/`, and IF
   the resolved path would fall outside `output/<run_id>/`, THEN THE
   File_Service SHALL reject the request with HTTP 403 and error code
   `forbidden`.
2. WHEN `GET /api/runs` is requested, THE File_Service SHALL list run
   directories by walking `output/` one level deep, including files directly in
   each run directory plus one level of subdirectory entries stored as
   `subdir/filename`, and SHALL exclude `agent-log.jsonl` and `checkpoints.json`.
3. WHEN `GET /api/runs/{id}/file?name=` is requested with a `name` that may
   include a single subdirectory segment (for example `subdir/file.md`), THE
   File_Service SHALL return the file content.
4. WHEN `POST /api/runs/{id}/file` is requested with file content, THE
   File_Service SHALL save the file atomically by writing a temporary file in
   the same directory and renaming it over the target.
5. WHEN `GET /api/runs/{id}/download/{file}` is requested, THE File_Service SHALL
   respond with the file content and a `Content-Disposition: attachment` header.

### Requirement 5: Console Boots Without AWS

**User Story:** As Mike, I want the Console to start without any AWS credentials
or SDK present, so that the shell never fails to boot for AWS-related reasons and
AWS is required only when a Run actually executes on the Python side.

#### Acceptance Criteria

1. WHERE the environment lacks AWS credentials and an AWS SDK, THE
   Bullpen_Console SHALL start, serve the UI, and list runs successfully.
2. THE Bullpen_Console binary SHALL depend only on the Go standard library and a
   single system-tray library, carrying no AWS SDK dependency.
3. WHEN `GET /api/health` is requested, THE HTTP_Server SHALL respond with
   `{"status":"ok"}`.
4. IF an internal error occurs while processing a `GET /api/health` request,
   THEN THE HTTP_Server SHALL still send a response carrying an error status
   rather than leaving the request unanswered.
5. WHERE AWS access is required, THE Bullpen SHALL provide it on the Python side
   only, surfaced to the Bullpen_Console as files in the run directory.

### Requirement 6: Vault-Only Suggestions with Title Search

**User Story:** As Mike, I want topic suggestions sourced only from my vault,
both a recency list and a title search, so that I can quickly pre-fill the Run
topic field without the Console ever calling AWS.

#### Acceptance Criteria

1. WHEN a suggestion request is served, THE Suggestion_Service SHALL derive
   results solely from the vault filesystem and SHALL make no AWS call.
2. WHEN `GET /api/suggestions` is requested with a `limit`, THE
   Suggestion_Service SHALL return up to `limit` notes from `06-permanent/` and
   `00-inbox/` ordered by modification time descending.
3. WHEN building the recency list, THE Suggestion_Service SHALL derive each
   topic from the permanent-note filename with any leading numeric ID stripped,
   or from the inbox note's first `# ` heading, and SHALL deduplicate entries by
   lowercased topic.
4. WHEN `GET /api/suggestions/search?q=` is requested, THE Suggestion_Service
   SHALL match `q` case-insensitively against the derived title of every `*.md`
   note under the vault and SHALL return up to the configured result cap.
5. IF `VAULT_PATH` does not resolve to an existing directory, THEN THE
   Suggestion_Service SHALL return an empty list with a `warning` field rather
   than returning an error.
6. THE Suggestion_Service SHALL read `VAULT_PATH` at call time.
7. WHERE a suggestion is selected, THE Bullpen_Console SHALL pre-fill the
   free-text topic field while still accepting any free-text topic value.

### Requirement 7: Starting a Run

**User Story:** As Mike, I want to start a Run from the Console with a topic and
chosen outputs, so that the Bullpen pipeline executes for that topic while the
Console enforces a single active Run.

#### Acceptance Criteria

1. WHEN `POST /api/run` is received with a non-empty `topic` and a valid
   `outputs` value, THE Run_Manager SHALL generate a `run_id`, create
   `output/<run_id>/`, spawn the Headless_Runner, and respond with HTTP 202 and
   body `{run_id}`.
2. WHEN spawning the Headless_Runner, THE Run_Manager SHALL pass the arguments
   `--run-id`, `--topic`, `--outputs`, and `--output-dir` as an argument vector
   rather than a shell string, so a free-text topic cannot inject shell commands.
3. IF a Run is already active, THEN THE Run_Manager SHALL respond with HTTP 409
   and SHALL not start a second Run.
4. IF the request body fails validation, including an empty `topic` or an
   `outputs` value that is neither `["all"]` nor a subset of
   {`blog`, `youtube`, `cfp`, `usergroup`, `digest`}, THEN THE HTTP_Server SHALL
   respond with HTTP 422.
5. WHILE the Headless_Runner subprocess runs, THE Run_Manager SHALL capture its
   stdout and stderr to `output/<run_id>/runner.stderr.log`.
6. IF the Headless_Runner fails to spawn, THEN THE HTTP_Server SHALL respond with
   HTTP 500 and body `{"error":"spawn_failed","detail":<message>}`, return no
   `run_id`, and mark no Run active.

### Requirement 8: dev.to Publishing

**User Story:** As Mike, I want to publish a Run's `post.md` to dev.to from the
Console over a plain HTTPS call, so that I can ship an approved article without
any AWS involvement.

#### Acceptance Criteria

1. WHEN `POST /api/publish/devto` is requested for a `run_id`, THE
   Devto_Publisher SHALL locate `post.md` at `output/<run_id>/post.md` or, if
   absent there, at the nested path `output/<run_id>/<date-slug>/post.md`.
2. WHEN publishing, THE Devto_Publisher SHALL send an HTTP POST to
   `https://dev.to/api/articles` with header `api-key: <DEVTO_API_KEY>` and body
   `{"article": {title, body_markdown, tags, published}}`.
3. WHEN dev.to responds with HTTP 201, THE Devto_Publisher SHALL treat the
   article as published and return `{success:true, url, id}`, even if delivering
   that success response to the client subsequently fails, and SHALL NOT
   additionally verify the response body fields before treating the publish as
   successful (HTTP 201 alone is authoritative; decided in issue #36).
4. IF dev.to responds with a non-201 status, THEN THE Devto_Publisher SHALL
   return `{success:false, status_code, error}` and THE HTTP_Server SHALL respond
   with HTTP 502.
5. IF the HTTPS request fails at the network level, THEN THE Devto_Publisher
   SHALL return `{success:false, error}`.
6. THE Devto_Publisher SHALL read `DEVTO_API_KEY` from the environment and SHALL
   keep it out of all log output.

### Requirement 9: Shared Wiring Factory Refactor (Python)

**User Story:** As a maintainer, I want the triplicated agent wiring collapsed
into one `build_agent_callables(...)` factory, so that the three pipeline entry
points share one wiring path and the existing pipeline tests still pass.

#### Acceptance Criteria

1. THE Wiring_Factory SHALL build every agent callable required by
   `run_pipeline()` — `researcher_fn`, `desk_editor_fn`, `writer_fn`,
   `subeditor_fn`, `publisher_fn`, `approval_fn`, `log_fn`, and `checkpoint_fn` —
   in a single module.
2. THE Wiring_Factory SHALL accept the `approval_fn` as a parameter so each entry
   point supplies its own gate.
3. WHERE invoked by the terminal CLI, the Headless_Runner, or the legacy GUI, THE
   Wiring_Factory SHALL produce equivalent callable sets that differ only in the
   supplied `approval_fn`.
4. THE Wiring_Factory SHALL preserve the existing AWS-backed callables (Bedrock
   inference, DynamoDB log and checkpoint writes, S3/SES publisher) unchanged.
5. WHEN the existing `pytest` and Hypothesis suites are run before and after the
   refactor, THE refactored Bullpen SHALL produce the same test outcomes with no
   regressions.

### Requirement 10: Headless Runner Entry Point (Python)

**User Story:** As the Console, I want a silent headless Python entry point I can
spawn per Run, so that I drive the pipeline through files alone and always
observe a terminal event.

#### Acceptance Criteria

1. WHEN invoked, THE Headless_Runner SHALL accept the arguments `--topic`,
   `--outputs`, `--run-id`, and `--output-dir`.
2. THE Headless_Runner SHALL wire the pipeline with the control-file
   `approval_fn` that polls `approval-decision.json`.
3. WHILE operating normally, THE Headless_Runner SHALL emit no output to stdout,
   communicating progress solely through `agent-log.jsonl`; and WHEN abnormal
   operation occurs, including handling a recoverable error or an exception, THE
   Headless_Runner MAY emit diagnostic or error output to stdout, even when the
   pipeline ultimately finishes successfully.
4. IF the pipeline raises an unhandled exception, THEN THE Headless_Runner SHALL
   append a synthetic `pipeline_complete` event with `status` of `error` and a
   `traceback` to `agent-log.jsonl`.
5. WHEN the pipeline finishes successfully, THE Headless_Runner SHALL exit with
   code 0; and WHERE the pipeline does not finish successfully, including when an
   unhandled exception prevents completion, THE Headless_Runner SHALL exit with a
   nonzero code.

### Requirement 11: Native Tray, Port, and Browser

**User Story:** As Mike, I want a native system tray, automatic free-port
selection, and browser launch, so that the Console replaces pystray and the
`netstat`/`taskkill` dance without killing other processes.

#### Acceptance Criteria

1. WHEN the Bullpen_Console starts, THE Desktop SHALL show a system tray with an
   "Open Bullpen" item as the default action and a "Quit" item.
2. WHEN no explicit port selection is configured, THE Desktop SHALL be able to
   start listening using a default port; and WHEN selecting a listening port,
   THE Desktop SHALL attempt the preferred port first, and IF the preferred port
   is already bound, THEN THE Desktop SHALL bind an OS-assigned free port instead
   of terminating the process that holds the preferred port.
3. WHEN the server is listening, THE Desktop SHALL open the default browser at
   the actual chosen URL.

### Requirement 12: HTTP Server Surface, Loopback Binding, and Error Shape

**User Story:** As Mike, I want the Go Console to reproduce the Flask Console's
API surface on a loopback-only listener with a consistent error shape, so that
the UI behaves the same and the server is never network-exposed.

#### Acceptance Criteria

1. THE HTTP_Server SHALL bind exclusively to the loopback interface `127.0.0.1`.
2. WHEN a request to an API endpoint (the `/api/...` surface) fails, THE
   HTTP_Server SHALL return a JSON body of the shape
   `{"error": <code>, "detail": <message>}`; and WHEN an error occurs serving a
   UI endpoint (`GET /` and `/static/...`), THE HTTP_Server SHALL return an HTML
   error response rather than JSON.
3. THE HTTP_Server SHALL expose the endpoints `POST /api/run`,
   `GET /api/run/status`, `POST /api/run/approve`, `POST /api/run/reject`,
   `GET /api/runs`, `GET /api/runs/{id}/file`, `POST /api/runs/{id}/file`,
   `GET /api/runs/{id}/download/{file}`, `GET /api/suggestions`,
   `GET /api/suggestions/search`, `POST /api/publish/devto`, `GET /api/health`,
   `GET /`, and `/static/...`.
4. IF `POST /api/run/approve` or `POST /api/run/reject` is received when no
   Approval_Gate is currently awaiting a decision, THEN THE HTTP_Server SHALL
   respond with HTTP 409 and body
   `{"error":"conflict","detail":"No approval gate is currently waiting."}`.
5. WHEN serving `GET /` and `/static/...`, THE HTTP_Server SHALL serve the UI
   assets embedded in the binary via `embed.FS`.

### Requirement 13: Legacy Flask Console Retained in Parallel

**User Story:** As Mike, I want the existing Flask Console kept working alongside
the Go Console during the transition, so that I retain a proven fallback until
the Go Console has handled several real Runs.

#### Acceptance Criteria

1. WHILE the Go Bullpen_Console is being proven over real Runs, THE
   Legacy_Flask_Console (`scripts/gui/app.py`, `run_gui.py`, `log_tailer.py`,
   `bullpen.bat`) SHALL remain operational.
2. WHERE the Wiring_Factory refactor changes shared Python wiring, THE
   Legacy_Flask_Console SHALL continue to function using the `threading.Event`
   approval gate.
3. THE deletion of the Legacy_Flask_Console SHALL be deferred to a follow-up and
   is out of scope for this rewrite.
