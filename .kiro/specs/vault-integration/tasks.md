# Implementation Plan: Vault Integration

## Overview

Wire a bidirectional vault connection into the Magic Content Engine using plain Python file I/O.
The implementation touches four existing files (`config.py`, `writing_agent.py`, `orchestrator.py`,
and two test files) and adds one new module (`vault.py`) plus one new test file (`test_vault.py`).

## Tasks

- [x] 1. Add `VAULT_PATH` constant to `config.py`
  - Append `VAULT_PATH: str = os.getenv("VAULT_PATH", "")` after the existing `DEVTO_USERNAME` constant
  - No other changes to `config.py`
  - _Requirements: 1.1_

- [x] 2. Create `magic_content_engine/vault.py`
  - [x] 2.1 Implement `VaultContext` dataclass and `VaultReader`
    - Define `VaultContext` dataclass with `permanent_notes: str` and `project_note: str` fields
    - Implement `VaultReader.__init__(self, vault_path: str)` storing `self._root = Path(vault_path)`
    - Implement `VaultReader.load()` returning a `VaultContext`
    - Implement `VaultReader._read_permanent_notes()`: iterate `sorted(permanent_dir.iterdir())`,
      include only direct `.md` files (`path.is_file() and path.suffix == ".md"`), read with
      `encoding="utf-8"`, log `WARNING` and skip on `OSError`, join with `"\n\n"`
    - Implement `VaultReader._read_project_note()`: read
      `01-projects/magic-content-engine.md` with `encoding="utf-8"`, return `""` and log
      `WARNING` if the file does not exist
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3, 7.1, 7.3, 7.4_

  - [x] 2.2 Implement `VaultWriter`
    - Implement `VaultWriter.__init__(self, vault_path: str)` storing `self._root = Path(vault_path)`
    - Implement `VaultWriter.write_summary(*, run_date, articles_found, articles_kept,
      confirmed_titles, selected_outputs, gate_decisions, errors)`:
      - Create `00-inbox/` with `mkdir(parents=True, exist_ok=True)`
      - Build Markdown content with sections in order: `# Magic Content Engine — Run YYYY-MM-DD`,
        `## Articles scored`, `## Topics covered`, `## Outputs generated`,
        `## Publish gate decisions`, `## Errors`
      - Write to `00-inbox/MCE-run-{run_date.isoformat()}.md` with `encoding="utf-8"` (overwrites)
      - Catch `OSError`, log at `ERROR` level, return silently — never raise
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 7.2, 7.3, 7.4_

- [x] 3. Add `vault_context` field to `WritingContext` and update prompt builders in `writing_agent.py`
  - [x] 3.1 Add `vault_context: str | None = None` field to `WritingContext` dataclass (after `slug`)
    - _Requirements: 4.1_

  - [x] 3.2 Add `_append_vault_context()` helper and update all `build_*_prompt()` functions
    - Add module-level helper:
      ```python
      def _append_vault_context(parts: list[str], vault_context: str | None) -> None:
          if vault_context is not None:
              parts.append("\n## Vault context\n")
              parts.append(vault_context)
      ```
    - Call `_append_vault_context(parts, context.vault_context)` at the end of each of the five
      `build_*_prompt()` functions (`build_blog_prompt`, `build_youtube_prompt`, `build_cfp_prompt`,
      `build_usergroup_prompt`, `build_digest_prompt`) before the final `return "\n".join(parts)`
    - _Requirements: 4.2, 4.3, 4.4_

- [x] 4. Update `orchestrator.py`
  - [x] 4.1 Add `vault_path` field to `WorkflowDependencies`
    - Add `vault_path: str = field(default_factory=lambda: config.VAULT_PATH)` after `unattended`
    - Import `field` from `dataclasses` if not already imported
    - _Requirements: 6.5_

  - [x] 4.2 Add `_format_vault_context()` helper to `orchestrator.py`
    - Add module-level function:
      ```python
      def _format_vault_context(vc: "vault.VaultContext") -> str:
          parts = []
          if vc.permanent_notes:
              parts.append("### Permanent notes\n\n" + vc.permanent_notes)
          if vc.project_note:
              parts.append("### Content backlog\n\n" + vc.project_note)
          return "\n\n".join(parts)
      ```
    - _Requirements: 6.2_

  - [x] 4.3 Wire Step 1.5 (`vault_read`) into `run_workflow()`
    - Insert after the `load_memory` step (Step 2) and before `crawl_sources`:
      ```python
      _log_step("vault_read")
      vault_context_str: str | None = None
      if deps.vault_path and Path(deps.vault_path).is_dir():
          try:
              from magic_content_engine.vault import VaultReader
              vc = VaultReader(deps.vault_path).load()
              vault_context_str = _format_vault_context(vc)
          except Exception as exc:
              collector.add(StepError(step="vault_read", target=deps.vault_path, error_message=str(exc)))
      else:
          if deps.vault_path:
              logger.warning("VAULT_PATH '%s' does not exist — skipping vault integration", deps.vault_path)
          else:
              logger.warning("VAULT_PATH not set — skipping vault integration")
      ```
    - _Requirements: 1.2, 1.3, 6.1, 8.1, 8.2, 8.3_

  - [x] 4.4 Pass `vault_context=vault_context_str` when constructing `WritingContext` in Step 13
    - Add `vault_context=vault_context_str` to the `WritingContext(...)` constructor call
    - _Requirements: 6.2, 6.4_

  - [x] 4.5 Wire Step 21 (`vault_write`) into `run_workflow()` after `terminal_summary`
    - Insert after `_print_terminal_summary(...)`:
      ```python
      _log_step("vault_write")
      if deps.vault_path and Path(deps.vault_path).is_dir():
          try:
              from magic_content_engine.vault import VaultWriter
              VaultWriter(deps.vault_path).write_summary(
                  run_date=run_date,
                  articles_found=len(all_articles),
                  articles_kept=len(confirmed_articles),
                  confirmed_titles=[a.title for a in confirmed_articles if a.title],
                  selected_outputs=selected_outputs,
                  gate_decisions=[
                      {"filename": r.filename, "decision": r.decision.value}
                      for r in gate_results
                  ] if gate_results else [],
                  errors=collector.to_list(),
              )
          except Exception as exc:
              collector.add(StepError(step="vault_write", target=deps.vault_path, error_message=str(exc)))
      ```
    - _Requirements: 6.3, 6.4, 8.3_

- [x] 5. Checkpoint — ensure existing tests still pass
  - Run `pytest magic_content_engine/ -x -q` and confirm no regressions before proceeding.
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Write `magic_content_engine/test_vault.py`
  - [x] 6.1 Write unit tests for `VaultReader`
    - `test_vault_reader_empty_permanent_dir` — `06-permanent/` exists but has no `.md` files → `permanent_notes == ""`
    - `test_vault_reader_missing_permanent_dir` — `06-permanent/` absent → `permanent_notes == ""`
    - `test_vault_reader_missing_project_note` — `01-projects/magic-content-engine.md` absent → `project_note == ""`
    - `test_vault_reader_utf8` — file containing `"Kia ora — ngā mihi"` is read correctly
    - `test_vault_reader_no_recursion` — `.md` file inside a sub-folder of `06-permanent/` is NOT included
    - _Requirements: 2.1, 2.2, 2.4, 2.5, 3.1, 3.2_

  - [x] 6.2 Write unit tests for `VaultWriter`
    - `test_vault_writer_creates_inbox_dir` — `00-inbox/` does not exist → created automatically
    - `test_vault_writer_overwrites_existing_file` — calling `write_summary()` twice produces the same file
    - `test_vault_writer_utf8` — summary with non-ASCII characters round-trips correctly
    - `test_vault_writer_all_sections_present` — output contains all six required headings in order
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.6_

  - [x] 6.3 Write unit tests for config and orchestrator integration points
    - `test_config_vault_path_default` — `VAULT_PATH` env var absent → `config.VAULT_PATH == ""`
    - `test_workflow_dependencies_vault_path` — `WorkflowDependencies` accepts injected `vault_path`
    - _Requirements: 1.1, 6.5_

  - [ ]* 6.4 Write property test: Property 1 — permanent notes round-trip
    - **Property 1: Permanent notes round-trip**
    - Use `@given(file_contents=lists(text(min_size=1), min_size=0, max_size=10))`
    - Write N random `.md` files to `06-permanent/` plus one `.md` in a sub-folder
    - Assert `permanent_notes` contains all file contents and excludes the sub-folder file
    - **Validates: Requirements 2.1, 2.5**

  - [ ]* 6.5 Write property test: Property 2 — project note round-trip
    - **Property 2: Project note round-trip**
    - Use `@given(content=text())`
    - Write content to `01-projects/magic-content-engine.md`, call `VaultReader.load()`
    - Assert `project_note == content` (byte-for-byte)
    - **Validates: Requirements 3.1, 3.3**

  - [ ]* 6.6 Write property test: Property 3 — VaultReader skips unreadable files
    - **Property 3: VaultReader skips unreadable files and continues**
    - Use `@given(readable=lists(text(min_size=1), min_size=1, max_size=5))`
    - Write readable files plus one file with `chmod(0o000)`; skip on Windows
    - Assert no exception raised and all readable contents present in `permanent_notes`
    - **Validates: Requirements 2.3**

  - [ ]* 6.7 Write property test: Property 6 — `write_summary` idempotent with required sections
    - **Property 6: write_summary produces correct idempotent output**
    - Use `@given(run_date=dates(), articles_found=integers(min_value=0, max_value=100), ...)`
    - Call `write_summary()` twice with identical args; read file after each call
    - Assert both calls produce identical content and all six section headings are present in order
    - **Validates: Requirements 5.1, 5.2, 5.4**

  - [ ]* 6.8 Write property test: Property 7 — VaultWriter survives filesystem errors
    - **Property 7: VaultWriter survives filesystem errors without raising**
    - Create `00-inbox/` with `chmod(0o444)` (read-only); skip on Windows
    - Call `VaultWriter(tmp_path).write_summary(...)` and assert no exception raised
    - **Validates: Requirements 5.5**

- [x] 7. Update `test_writing_agent.py` for `vault_context`
  - [x] 7.1 Update `_make_context()` helper — no change needed (field defaults to `None`)
    - Verify `_make_context()` still works without passing `vault_context`
    - Add `test_writing_context_vault_context_default` asserting the field defaults to `None`
    - _Requirements: 4.1_

  - [ ]* 7.2 Write property test: Property 4 — `vault_context` appears in prompt when non-None
    - **Property 4: vault_context appears in prompt when non-None**
    - Use `@given(vault_context=text(min_size=1), output_type=sampled_from(["blog","youtube","cfp","usergroup","digest"]))`
    - Build `WritingContext` with `vault_context` set, call the matching `build_*_prompt()`
    - Assert `"## Vault context"` in prompt, vault context text in prompt, `"### Content backlog"` in prompt
    - **Validates: Requirements 4.2, 4.4**

  - [ ]* 7.3 Write property test: Property 5 — `vault_context` absent from prompt when None
    - **Property 5: vault_context absent from prompt when None**
    - Use `@given(output_type=sampled_from(["blog","youtube","cfp","usergroup","digest"]))`
    - Build `WritingContext` with `vault_context=None`, call the matching `build_*_prompt()`
    - Assert `"## Vault context"` not in prompt
    - **Validates: Requirements 4.3**

- [x] 8. Update `test_orchestrator.py` for vault integration
  - [x] 8.1 Update `_make_deps()` to accept and pass through `vault_path`
    - Add `vault_path: str = ""` parameter to `_make_deps()` and include it in `WorkflowDependencies(...)`
    - _Requirements: 6.5_

  - [x] 8.2 Add `test_orchestrator_step_order` — verify `vault_read` and `vault_write` appear in step log
    - Run `run_workflow()` with a valid `tmp_path` vault directory
    - Assert `vault_read` appears in the step log after `load_memory`
    - Assert `vault_write` appears in the step log after `terminal_summary`
    - _Requirements: 6.1, 6.3_

  - [x] 8.3 Add `test_orchestrator_skips_vault_when_path_empty` — `vault_path=""` → `vault_context=None` for all `WritingContext` calls
    - Capture `WritingContext` instances passed to `llm_writer` stub
    - Assert all have `vault_context is None`
    - _Requirements: 6.4, 8.1_

  - [ ]* 8.4 Write property test: Property 8 — `vault_context` propagation through orchestrator
    - **Property 8: vault_context propagation through orchestrator**
    - Use `@given(vault_content=text(min_size=1))`
    - Set up a valid vault at `tmp_path` with known content, run orchestrator with `deps.vault_path = str(tmp_path)`
    - Capture `WritingContext` instances; assert all have `vault_context` containing the vault content
    - **Validates: Requirements 6.2, 6.4**

  - [ ]* 8.5 Write property test: Property 9 — orchestrator survives vault exceptions via ErrorCollector
    - **Property 9: Orchestrator survives vault exceptions via ErrorCollector**
    - Inject a `VaultReader` stub that always raises `RuntimeError`
    - Run `run_workflow()` and assert it returns a valid `AgentLog`
    - Assert `collector` contains a `StepError` with `step="vault_read"`
    - **Validates: Requirements 8.3**

- [x] 9. Final checkpoint — ensure all tests pass
  - Run `pytest magic_content_engine/ -x -q` and confirm the full suite is green.
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP
- Property tests use [Hypothesis](https://hypothesis.readthedocs.io/) — confirm it is listed in `pyproject.toml` before running
- File permission tests (Properties 3 and 7) should be skipped on Windows with `pytest.mark.skipif(sys.platform == "win32", ...)`
- All vault file I/O uses `pathlib.Path` and built-in `open()` — no MCP or HTTP dependencies
- `vault_context_str` is initialised to `None` before Step 1.5 so graceful degradation requires no extra branching in Step 13
