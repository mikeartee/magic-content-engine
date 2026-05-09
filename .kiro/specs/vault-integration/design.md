# Design Document: Vault Integration

## Overview

Vault Integration adds a bidirectional connection between the Magic Content Engine and a local
second-brain vault — a folder of Markdown files at the path given by `VAULT_PATH`. At run start
the engine reads permanent notes and the project note to give the Writing Sub-Agent richer context
(voice, niche, past decisions, ready-to-write ideas). At run end it writes a structured inbox note
summarising what happened.

The integration is entirely optional. When `VAULT_PATH` is absent or invalid the engine degrades
gracefully: `vault_context=None` is passed to `WritingContext` and the writer step is skipped.
This keeps the engine deployable on AgentCore Runtime (AWS) where no local vault exists.

All file I/O uses Python's built-in `open()` and `pathlib.Path` — no MCP server, no HTTP client.

---

## Architecture

```mermaid
flowchart TD
    subgraph Orchestrator["orchestrator.py — run_workflow()"]
        S2[Step 2: load_memory]
        S15[Step 1.5: vault_read]
        S7[Step 7: crawl_sources]
        S13[Step 13: generate_content]
        S20[Step 20: terminal_summary]
        S21[Step 21: vault_write]
    end

    subgraph vault_module["vault.py"]
        VR[VaultReader.load()]
        VW[VaultWriter.write_summary()]
    end

    subgraph writing["writing_agent.py"]
        WC[WritingContext\nvault_context: str | None]
        GC[generate_content()]
    end

    subgraph config["config.py"]
        VP[VAULT_PATH constant]
    end

    subgraph vault_fs["Local Vault (VAULT_PATH)"]
        P06["06-permanent/*.md"]
        P01["01-projects/magic-content-engine.md"]
        P00["00-inbox/MCE-run-YYYY-MM-DD.md"]
    end

    VP -->|injected via WorkflowDependencies.vault_path| S15
    S2 --> S15
    S15 -->|vault_path| VR
    VR -->|reads| P06
    VR -->|reads| P01
    VR -->|VaultContext| S15
    S15 --> S7
    S13 -->|vault_context=str or None| WC
    WC --> GC
    S20 --> S21
    S21 -->|vault_path + run metadata| VW
    VW -->|writes| P00
```

### Data flow summary

1. `config.py` exposes `VAULT_PATH` (env var, defaults to `""`).
2. `WorkflowDependencies` carries `vault_path: str` (defaults to `config.VAULT_PATH`).
3. Step 1.5 calls `VaultReader(vault_path).load()` → `VaultContext` dataclass.
4. Step 13 passes `vault_context` (the combined text string, or `None`) into each `WritingContext`.
5. `generate_content()` injects the vault context into the LLM prompt under `## Vault context`.
6. Step 21 calls `VaultWriter(vault_path).write_summary(...)` after the terminal summary.

---

## Components and Interfaces

### `magic_content_engine/vault.py` (new module)

#### `VaultContext` dataclass

```python
@dataclass
class VaultContext:
    permanent_notes: str   # combined text of all 06-permanent/*.md files
    project_note: str      # full text of 01-projects/magic-content-engine.md
```

#### `VaultReader`

```python
class VaultReader:
    def __init__(self, vault_path: str) -> None:
        self._root = Path(vault_path)

    def load(self) -> VaultContext:
        """Read permanent notes and project note from the vault.

        Returns a VaultContext with empty strings for any section that
        cannot be read. Never raises — all errors are logged at WARNING.
        """
        permanent_notes = self._read_permanent_notes()
        project_note = self._read_project_note()
        return VaultContext(
            permanent_notes=permanent_notes,
            project_note=project_note,
        )

    def _read_permanent_notes(self) -> str:
        """Read all .md files directly inside 06-permanent/ (non-recursive).

        Returns combined text separated by double newlines.
        Skips unreadable files with a WARNING log.
        """
        ...

    def _read_project_note(self) -> str:
        """Read 01-projects/magic-content-engine.md.

        Returns empty string and logs WARNING if the file does not exist.
        """
        ...
```

#### `VaultWriter`

```python
class VaultWriter:
    def __init__(self, vault_path: str) -> None:
        self._root = Path(vault_path)

    def write_summary(
        self,
        *,
        run_date: date,
        articles_found: int,
        articles_kept: int,
        confirmed_titles: list[str],
        selected_outputs: list[str],
        gate_decisions: list[dict],   # [{"filename": str, "decision": str}, ...]
        errors: list[dict],           # ErrorCollector.to_list() output
    ) -> None:
        """Write MCE-run-YYYY-MM-DD.md to 00-inbox/.

        Creates 00-inbox/ if it does not exist.
        Overwrites any existing file with the same name.
        Logs at ERROR and returns silently on any filesystem failure.
        """
        ...
```

### `magic_content_engine/config.py` (change)

Add one constant at the end of the existing file:

```python
# --- Vault ---
VAULT_PATH: str = os.getenv("VAULT_PATH", "")
```

### `magic_content_engine/writing_agent.py` (change)

Add `vault_context` field to `WritingContext`:

```python
@dataclass
class WritingContext:
    articles: list[ArticleWithCitation]
    output_type: str
    steering_base_path: str
    screenshots_path: str
    run_date: date
    slug: str
    vault_context: str | None = None   # NEW — injected by orchestrator
```

Inject vault context into every prompt builder. The shared helper:

```python
def _append_vault_context(parts: list[str], vault_context: str | None) -> None:
    """Append vault context section to prompt parts if present."""
    if vault_context is not None:
        parts.append("\n## Vault context\n")
        parts.append(vault_context)
```

Each `build_*_prompt` function calls `_append_vault_context(parts, context.vault_context)`
before returning `"\n".join(parts)`.

### `magic_content_engine/orchestrator.py` (change)

#### `WorkflowDependencies` — add `vault_path` field

```python
@dataclass
class WorkflowDependencies:
    ...
    vault_path: str = field(default_factory=lambda: config.VAULT_PATH)
```

#### `run_workflow()` — add Step 1.5 and Step 21

Step 1.5 (after `load_memory`, before `crawl_sources`):

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

Step 13 — pass `vault_context` when constructing `WritingContext`:

```python
ctx = WritingContext(
    articles=article_citation_pairs,
    output_type=output_type,
    steering_base_path=deps.steering_base_path,
    screenshots_path=screenshots_path,
    run_date=run_date,
    slug=slug,
    vault_context=vault_context_str,   # NEW
)
```

Step 21 (after `terminal_summary`):

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

---

## Data Models

### `VaultContext` (new, in `vault.py`)

| Field | Type | Description |
|---|---|---|
| `permanent_notes` | `str` | Combined text of all `06-permanent/*.md` files, separated by `\n\n` |
| `project_note` | `str` | Full text of `01-projects/magic-content-engine.md` |

### `WritingContext` (modified, in `writing_agent.py`)

| Field | Type | Default | Change |
|---|---|---|---|
| `articles` | `list[ArticleWithCitation]` | — | existing |
| `output_type` | `str` | — | existing |
| `steering_base_path` | `str` | — | existing |
| `screenshots_path` | `str` | — | existing |
| `run_date` | `date` | — | existing |
| `slug` | `str` | — | existing |
| `vault_context` | `str \| None` | `None` | **new** |

### `WorkflowDependencies` (modified, in `orchestrator.py`)

| Field | Type | Default | Change |
|---|---|---|---|
| ... | ... | ... | existing fields unchanged |
| `vault_path` | `str` | `config.VAULT_PATH` | **new** |

### Run Summary Note format

```markdown
# Magic Content Engine — Run YYYY-MM-DD

## Articles scored
Found: N  Kept: M

## Topics covered
- Article title 1
- Article title 2

## Outputs generated
- blog
- youtube

## Publish gate decisions
- post.md: approve
- script.md: hold

## Errors
None
```

---

## Key Algorithms

### `VaultReader._read_permanent_notes()` pseudocode

```
permanent_dir = self._root / "06-permanent"
if not permanent_dir.is_dir():
    return ""

parts = []
for path in sorted(permanent_dir.iterdir()):
    if path.is_file() and path.suffix == ".md":
        try:
            parts.append(path.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning("vault: cannot read %s: %s", path.name, exc)

return "\n\n".join(parts)
```

Note: `sorted()` ensures deterministic ordering across platforms.
`path.is_file()` check excludes sub-directories without recursing.

### `VaultReader._read_project_note()` pseudocode

```
project_file = self._root / "01-projects" / "magic-content-engine.md"
if not project_file.exists():
    logger.warning("vault: project note not found at %s", project_file)
    return ""
return project_file.read_text(encoding="utf-8")
```

### `VaultWriter.write_summary()` pseudocode

```
inbox_dir = self._root / "00-inbox"
inbox_dir.mkdir(parents=True, exist_ok=True)

filename = f"MCE-run-{run_date.isoformat()}.md"
dest = inbox_dir / filename

content = _build_summary_markdown(
    run_date, articles_found, articles_kept,
    confirmed_titles, selected_outputs, gate_decisions, errors
)

try:
    dest.write_text(content, encoding="utf-8")
except OSError as exc:
    logger.error("vault: failed to write run summary to %s: %s", dest, exc)
    return
```

### `_format_vault_context()` helper (in `orchestrator.py`)

```python
def _format_vault_context(vc: VaultContext) -> str:
    """Combine VaultContext fields into a single prompt-ready string."""
    parts = []
    if vc.permanent_notes:
        parts.append("### Permanent notes\n\n" + vc.permanent_notes)
    if vc.project_note:
        parts.append("### Content backlog\n\n" + vc.project_note)
    return "\n\n".join(parts)
```

This string is what gets stored in `vault_context_str` and injected into `WritingContext`.
The `## Vault context` heading is added by `_append_vault_context()` in the prompt builders,
keeping the orchestrator and writing agent concerns separate.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Permanent notes round-trip

*For any* vault directory containing N `.md` files directly inside `06-permanent/` (N ≥ 0), calling `VaultReader.load()` should return a `VaultContext` whose `permanent_notes` field contains the text of every readable file, and should NOT contain text from any `.md` file in a sub-folder of `06-permanent/`.

**Validates: Requirements 2.1, 2.5**

---

### Property 2: Project note round-trip

*For any* vault directory containing `01-projects/magic-content-engine.md` with arbitrary Markdown content, calling `VaultReader.load()` should return a `VaultContext` whose `project_note` field is byte-for-byte identical to the file's content.

**Validates: Requirements 3.1, 3.3**

---

### Property 3: VaultReader skips unreadable files and continues

*For any* vault directory where one or more files in `06-permanent/` are unreadable, calling `VaultReader.load()` should return a `VaultContext` containing the text of all readable files and should not raise an exception.

**Validates: Requirements 2.3**

---

### Property 4: vault_context appears in prompt when non-None

*For any* non-None `vault_context` string and any output type, the LLM prompt produced by the corresponding `build_*_prompt()` function should contain the heading `## Vault context` followed by the vault context text, and should contain a `### Content backlog` sub-section.

**Validates: Requirements 4.2, 4.4**

---

### Property 5: vault_context absent from prompt when None

*For any* `WritingContext` where `vault_context` is `None`, the LLM prompt produced by the corresponding `build_*_prompt()` function should NOT contain the string `## Vault context`.

**Validates: Requirements 4.3**

---

### Property 6: write_summary produces correct idempotent output

*For any* run metadata (run date, article counts, titles, outputs, gate decisions, errors), calling `VaultWriter.write_summary()` should create `00-inbox/MCE-run-YYYY-MM-DD.md` containing all required section headings in order (`# Magic Content Engine — Run`, `## Articles scored`, `## Topics covered`, `## Outputs generated`, `## Publish gate decisions`, `## Errors`). Calling it a second time with the same arguments should produce an identical file (idempotent overwrite).

**Validates: Requirements 5.1, 5.2, 5.4**

---

### Property 7: VaultWriter survives filesystem errors without raising

*For any* vault path where `00-inbox/` is not writable, calling `VaultWriter.write_summary()` should return normally without raising an exception, and the error should be logged at `ERROR` level.

**Validates: Requirements 5.5**

---

### Property 8: vault_context propagation through orchestrator

*For any* valid vault path, the `vault_context` value passed to each `WritingContext` in Step 13 should equal the formatted string derived from `VaultReader.load()`. When vault integration is skipped (empty or invalid path), `vault_context` should be `None` for every `WritingContext` and no vault file should be written.

**Validates: Requirements 6.2, 6.4**

---

### Property 9: Orchestrator survives vault exceptions via ErrorCollector

*For any* vault operation that raises an unhandled exception, the orchestrator's `run_workflow()` should complete all non-vault steps, record the exception in the `ErrorCollector`, and return a valid `AgentLog`.

**Validates: Requirements 8.3**

---

## Error Handling

| Scenario | Component | Behaviour |
|---|---|---|
| `VAULT_PATH` not set | Orchestrator | Log `WARNING`, set `vault_context_str = None`, skip vault write |
| `VAULT_PATH` set but path does not exist | Orchestrator | Log `WARNING`, set `vault_context_str = None`, skip vault write |
| `06-permanent/` directory missing | `VaultReader` | Return `""` for `permanent_notes` silently |
| Individual `.md` file unreadable | `VaultReader` | Log `WARNING` with filename, skip file, continue |
| `01-projects/magic-content-engine.md` missing | `VaultReader` | Log `WARNING`, return `""` for `project_note` |
| `VaultReader.load()` raises unexpectedly | Orchestrator | Catch, add `StepError(step="vault_read")`, continue with `vault_context_str = None` |
| `00-inbox/` creation fails | `VaultWriter` | `mkdir` raises `OSError` → caught, logged at `ERROR`, return silently |
| File write fails | `VaultWriter` | Log `ERROR`, return silently — no exception propagates |
| `VaultWriter.write_summary()` raises unexpectedly | Orchestrator | Catch, add `StepError(step="vault_write")`, continue |

All vault errors are non-fatal. The `ErrorCollector` accumulates them and they appear in the
terminal summary and `agent-log.json` under the `errors` key.

---

## Testing Strategy

### Dual testing approach

Unit tests cover specific examples, edge cases, and integration points. Property-based tests
verify universal properties across randomly generated inputs. Both are required.

### Unit tests (specific examples and edge cases)

- `test_vault_reader_empty_permanent_dir` — `06-permanent/` exists but contains no `.md` files → `permanent_notes == ""`
- `test_vault_reader_missing_project_note` — `01-projects/magic-content-engine.md` absent → `project_note == ""`
- `test_vault_reader_utf8` — file containing non-ASCII characters (e.g. `"Kia ora — ngā mihi"`) is read correctly
- `test_vault_writer_creates_inbox_dir` — `00-inbox/` does not exist → created automatically
- `test_vault_writer_utf8` — summary containing non-ASCII characters is written and read back correctly
- `test_config_vault_path_default` — `VAULT_PATH` env var absent → `config.VAULT_PATH == ""`
- `test_writing_context_vault_context_default` — `WritingContext` constructed without `vault_context` → field is `None`
- `test_orchestrator_step_order` — step log contains `vault_read` after `load_memory` and `vault_write` after `terminal_summary`
- `test_workflow_dependencies_vault_path` — `WorkflowDependencies` accepts injected `vault_path`

### Property-based tests

Uses [Hypothesis](https://hypothesis.readthedocs.io/) (already available in the Python ecosystem).
Each property test runs a minimum of 100 iterations.

**Property 1 — Permanent notes round-trip**
```
# Feature: vault-integration, Property 1: permanent notes round-trip
@given(file_contents=lists(text(min_size=1), min_size=0, max_size=10))
def test_permanent_notes_round_trip(tmp_path, file_contents):
    # Write N random .md files to 06-permanent/
    # Also write a .md file inside a sub-folder (should be excluded)
    # Call VaultReader(tmp_path).load()
    # Assert permanent_notes contains all file contents
    # Assert sub-folder file content is NOT in permanent_notes
```

**Property 2 — Project note round-trip**
```
# Feature: vault-integration, Property 2: project note round-trip
@given(content=text())
def test_project_note_round_trip(tmp_path, content):
    # Write content to 01-projects/magic-content-engine.md
    # Call VaultReader(tmp_path).load()
    # Assert project_note == content
```

**Property 3 — VaultReader skips unreadable files**
```
# Feature: vault-integration, Property 3: skip unreadable files
@given(readable=lists(text(min_size=1), min_size=1, max_size=5))
def test_reader_skips_unreadable(tmp_path, readable):
    # Write readable files + one file with mode 000
    # Call VaultReader(tmp_path).load()
    # Assert no exception raised
    # Assert all readable file contents present in permanent_notes
```

**Property 4 — vault_context in prompt when non-None**
```
# Feature: vault-integration, Property 4: vault_context in prompt
@given(vault_context=text(min_size=1), output_type=sampled_from(["blog","youtube","cfp","usergroup","digest"]))
def test_vault_context_in_prompt(vault_context, output_type):
    # Build WritingContext with vault_context set
    # Call appropriate build_*_prompt()
    # Assert "## Vault context" in prompt
    # Assert vault_context text in prompt
    # Assert "### Content backlog" in prompt
```

**Property 5 — vault_context absent when None**
```
# Feature: vault-integration, Property 5: vault_context absent when None
@given(output_type=sampled_from(["blog","youtube","cfp","usergroup","digest"]))
def test_vault_context_absent_when_none(output_type):
    # Build WritingContext with vault_context=None
    # Call appropriate build_*_prompt()
    # Assert "## Vault context" not in prompt
```

**Property 6 — write_summary idempotent with required sections**
```
# Feature: vault-integration, Property 6: write_summary idempotent
@given(
    run_date=dates(),
    articles_found=integers(min_value=0, max_value=100),
    articles_kept=integers(min_value=0, max_value=100),
    titles=lists(text(min_size=1), max_size=10),
    outputs=lists(sampled_from(["blog","youtube","cfp","usergroup","digest"]), max_size=5),
)
def test_write_summary_idempotent(tmp_path, run_date, articles_found, articles_kept, titles, outputs):
    # Call write_summary() twice with same args
    # Read file after each call
    # Assert both calls produce identical content
    # Assert all required section headings present in order
```

**Property 7 — VaultWriter survives filesystem errors**
```
# Feature: vault-integration, Property 7: writer survives errors
def test_writer_survives_read_only_inbox(tmp_path):
    # Create 00-inbox/ with mode 444 (read-only)
    # Call VaultWriter(tmp_path).write_summary(...)
    # Assert no exception raised
```

**Property 8 — vault_context propagation through orchestrator**
```
# Feature: vault-integration, Property 8: vault_context propagation
@given(vault_content=text(min_size=1))
def test_vault_context_propagation(tmp_path, vault_content):
    # Set up a valid vault at tmp_path with known content
    # Run orchestrator with deps.vault_path = str(tmp_path)
    # Capture WritingContext instances passed to llm_writer stub
    # Assert all have vault_context != None and containing vault_content
```

**Property 9 — Orchestrator survives vault exceptions**
```
# Feature: vault-integration, Property 9: orchestrator survives vault exceptions
def test_orchestrator_survives_vault_exception(tmp_path):
    # Inject a VaultReader that always raises RuntimeError
    # Run orchestrator
    # Assert run_workflow() returns AgentLog
    # Assert collector contains a StepError with step="vault_read"
```
