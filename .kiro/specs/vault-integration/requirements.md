# Requirements Document

## Introduction

Vault Integration adds a bidirectional connection between the Magic Content Engine and the local
second-brain vault (a folder of Markdown files at the path specified by `VAULT_PATH`). At run
start the engine reads permanent notes and the project backlog to give the Writing Sub-Agent
richer context — voice, niche, past decisions, and ready-to-write ideas. At run end it writes a
structured inbox note summarising what happened. If `VAULT_PATH` is not set the integration is
skipped gracefully so the engine continues to work in environments without a local vault (e.g.
AgentCore Runtime on AWS).

## Glossary

- **Vault**: The local folder of Markdown files at the path given by `VAULT_PATH`. Organised into
  sub-folders: `00-inbox/`, `01-projects/`, `04-knowledge/`, `06-permanent/`, `07-templates/`.
- **Vault_Reader**: The component responsible for reading Markdown files from the vault at run
  start.
- **Vault_Writer**: The component responsible for writing the run summary note to `00-inbox/` at
  run end.
- **Permanent_Notes**: Atomic Zettelkasten notes stored in `06-permanent/`. Each note contains one
  idea written to last.
- **Project_Note**: The file `01-projects/magic-content-engine.md` inside the vault. Contains
  active project context and the content backlog.
- **Content_Backlog**: The list of ready-to-write ideas found inside the Project_Note.
- **Vault_Context**: The combined payload — Permanent_Notes text and Project_Note text — passed to
  the Writing Sub-Agent.
- **Run_Summary_Note**: The structured Markdown file written to `00-inbox/` after a run completes.
- **Orchestrator**: The existing `orchestrator.py` component that coordinates the end-to-end
  workflow.
- **Writing_Sub_Agent**: The existing `writing_agent.py` component that generates content output.
- **VAULT_PATH**: Environment variable holding the absolute path to the vault folder.

---

## Requirements

### Requirement 1: Vault Path Configuration

**User Story:** As a developer running the Magic Content Engine, I want the vault path loaded from
an environment variable, so that the integration works on any machine without code changes.

#### Acceptance Criteria

1. THE `config.py` module SHALL expose a `VAULT_PATH` constant loaded from the `VAULT_PATH`
   environment variable, defaulting to an empty string when the variable is not set.
2. WHEN `VAULT_PATH` is an empty string, THE Orchestrator SHALL log a warning at `WARNING` level
   and continue the run without vault integration.
3. WHEN `VAULT_PATH` is set to a path that does not exist on the filesystem, THE Orchestrator
   SHALL log a warning at `WARNING` level and continue the run without vault integration.
4. THE Vault_Reader and Vault_Writer SHALL accept `vault_path` as a parameter rather than reading
   `VAULT_PATH` directly, so that tests can inject any path without modifying environment state.

---

### Requirement 2: Read Permanent Notes at Run Start

**User Story:** As the Writing Sub-Agent, I want access to the permanent notes from the vault, so
that generated content is grounded in real builds, lessons, and insights.

#### Acceptance Criteria

1. WHEN `VAULT_PATH` resolves to a valid directory, THE Vault_Reader SHALL read all `.md` files
   found directly inside `06-permanent/` and return their combined text.
2. WHEN `06-permanent/` contains no `.md` files, THE Vault_Reader SHALL return an empty string for
   the permanent notes section without raising an error.
3. WHEN a `.md` file inside `06-permanent/` cannot be read due to a filesystem error, THE
   Vault_Reader SHALL log the filename and error at `WARNING` level, skip that file, and continue
   reading the remaining files.
4. THE Vault_Reader SHALL read files using UTF-8 encoding.
5. THE Vault_Reader SHALL NOT recurse into sub-folders of `06-permanent/`.

---

### Requirement 3: Read Project Note and Content Backlog at Run Start

**User Story:** As the Writing Sub-Agent, I want access to the project note and content backlog,
so that I can reference ready-to-write ideas when suggesting angles and framing.

#### Acceptance Criteria

1. WHEN `VAULT_PATH` resolves to a valid directory, THE Vault_Reader SHALL read the file at
   `01-projects/magic-content-engine.md` and return its full text.
2. WHEN `01-projects/magic-content-engine.md` does not exist, THE Vault_Reader SHALL return an
   empty string for the project note section and log a warning at `WARNING` level.
3. THE Vault_Reader SHALL return the project note text unchanged so that the Writing Sub-Agent
   receives the Content_Backlog in its original Markdown format.

---

### Requirement 4: Vault Context Passed to Writing Sub-Agent

**User Story:** As the Writing Sub-Agent, I want the vault context injected into my prompt, so
that generated content reflects the author's voice, niche, and backlog ideas.

#### Acceptance Criteria

1. THE `WritingContext` dataclass SHALL include an optional `vault_context` field of type
   `str | None`, defaulting to `None`.
2. WHEN `vault_context` is not `None`, THE Writing_Sub_Agent SHALL include the vault context in
   the LLM prompt under a clearly labelled section heading (e.g. `## Vault context`).
3. WHEN `vault_context` is `None`, THE Writing_Sub_Agent SHALL generate content without a vault
   context section, producing output identical to the pre-integration behaviour.
4. THE Writing_Sub_Agent SHALL include the Content_Backlog from the vault context in the prompt
   under a sub-section labelled `### Content backlog` so the model can reference it for angle and
   framing suggestions.
5. WHEN voice-rule validation detects violations in generated content, THE Writing_Sub_Agent SHALL
   log the violations at `WARNING` level regardless of whether vault context was provided.

---

### Requirement 5: Write Run Summary Note to Inbox

**User Story:** As the vault owner, I want a structured note written to `00-inbox/` after each
run, so that I have a searchable record of what the engine covered and decided.

#### Acceptance Criteria

1. WHEN a run completes and `VAULT_PATH` resolves to a valid directory, THE Vault_Writer SHALL
   write a Markdown file to `00-inbox/` with a filename in the format
   `MCE-run-YYYY-MM-DD.md` where `YYYY-MM-DD` is the run date.
2. THE Run_Summary_Note SHALL contain the following sections in order:
   a. A top-level heading: `# Magic Content Engine — Run YYYY-MM-DD`
   b. `## Articles scored` — count of articles found and count kept after scoring and deduplication
   c. `## Topics covered` — list of article titles that were confirmed for content generation
   d. `## Outputs generated` — list of output types selected (blog, youtube, cfp, usergroup, digest)
   e. `## Publish gate decisions` — list of filenames with their gate decision (approve / skip /
      hold / review)
   f. `## Errors` — list of step errors from the run, or the text `None` if the run was clean
3. WHEN `00-inbox/` does not exist inside the vault, THE Vault_Writer SHALL create the directory
   before writing the file.
4. WHEN a file with the same name already exists in `00-inbox/`, THE Vault_Writer SHALL overwrite
   it.
5. IF the Vault_Writer encounters a filesystem error while writing, THEN THE Vault_Writer SHALL
   log the error at `ERROR` level and continue without raising an exception, so the run summary
   is not lost.
6. THE Vault_Writer SHALL write files using UTF-8 encoding.

---

### Requirement 6: Orchestrator Integration

**User Story:** As a developer, I want vault read and write wired into the existing orchestrator
workflow, so that vault integration happens automatically on every run without manual steps.

#### Acceptance Criteria

1. THE Orchestrator SHALL load Vault_Context as the first step after loading voice profile and
   covered URLs (current Step 2), before crawling begins.
2. THE Orchestrator SHALL pass Vault_Context to `WritingContext` when constructing each content
   generation call in Step 13.
3. THE Orchestrator SHALL call Vault_Writer as the final step after the terminal summary (after
   current Step 20), passing the run date, article counts, confirmed article titles, selected
   outputs, publish gate decisions, and error list.
4. WHEN vault integration is skipped due to missing or invalid `VAULT_PATH`, THE Orchestrator
   SHALL pass `vault_context=None` to `WritingContext` and SHALL skip the Vault_Writer call.
5. THE `WorkflowDependencies` dataclass SHALL include an optional `vault_path` field of type
   `str` defaulting to the value of `config.VAULT_PATH`, so that tests can inject a temporary
   directory path.

---

### Requirement 7: No MCP Dependency

**User Story:** As a developer deploying the engine on AgentCore Runtime, I want vault reads and
writes to use plain Python file I/O, so that there is no MCP server dependency at runtime.

#### Acceptance Criteria

1. THE Vault_Reader SHALL read files using Python's built-in `open()` and `pathlib.Path`
   primitives only.
2. THE Vault_Writer SHALL write files using Python's built-in `open()` and `pathlib.Path`
   primitives only.
3. THE vault integration module SHALL NOT import any MCP client library or external HTTP client
   for vault operations.
4. THE vault integration module SHALL be importable in an environment where no MCP server is
   running.

---

### Requirement 8: Graceful Degradation

**User Story:** As a developer running the engine in a CI environment or on AgentCore Runtime
without a local vault, I want the engine to run normally when vault integration is unavailable,
so that the absence of a vault never blocks content generation.

#### Acceptance Criteria

1. WHEN `VAULT_PATH` is not set, THE Orchestrator SHALL complete all non-vault workflow steps
   without modification to existing behaviour.
2. WHEN `VAULT_PATH` is set but the vault directory is unreachable, THE Orchestrator SHALL
   complete all non-vault workflow steps without modification to existing behaviour.
3. IF any vault read or write operation raises an unhandled exception, THEN THE Orchestrator
   SHALL catch the exception, record it via `ErrorCollector`, and continue the run.
4. THE existing test suite SHALL continue to pass without requiring a vault directory to be
   present on the test runner's filesystem.
