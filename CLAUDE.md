# Magic Content Engine — Claude Code Context

## What this is

A weekly content research and generation pipeline for an AWS Community Builder. It crawls 9 sources, scores articles with Claude Haiku, generates blog posts / YouTube scripts / CFP proposals / user group sessions / digest emails with Claude Sonnet, runs a publish gate, and uploads approved outputs to S3.

## Current state: code complete, nothing deployed

All pipeline logic is implemented and tested. What's missing is the **wiring layer** — a concrete factory that constructs `WorkflowDependencies` with real AWS-backed implementations and calls `run_workflow()`.

The three explicit stubs are:
- `magic_content_engine/orchestrator.py` → `main()` logs "CLI stub exiting" and returns without calling `run_workflow()`
- `magic_content_engine/gateway.py` → `invoke_content_run_handler()` raises `NotImplementedError`
- `magic_content_engine/identity.py` → `AgentCoreIdentityProvider.get_credential()` raises `NotImplementedError`

There is no deployed infrastructure: no EventBridge rule, no Lambda, no AgentCore agents, no EC2.

## Architecture decision needed: AgentCore vs standard AWS

The codebase was designed for **AWS AgentCore** (managed Runtime, Browser, Memory, Identity, Observability). None of that is deployed. The alternative is standard AWS equivalents:

| AgentCore service | Standard AWS replacement |
|---|---|
| AgentCore Memory | DynamoDB |
| AgentCore Browser | Playwright (Lambda layer) or drop JS-rendered sources |
| AgentCore Identity | AWS Secrets Manager |
| AgentCore Observability | CloudWatch |
| AgentCore Runtime | Lambda |
| EventBridge rule | EventBridge Scheduler |

**Browser caveat**: 4 of 9 research sources are JS-rendered React sites (community.aws, repost.aws, kiro.dev, strandsagents.com). Plain HTTP requests won't work. Options: Playwright in a Lambda layer, use AgentCore Browser, or drop those sources and rely on the 5 that work with HTTP (GitHub API × 2, aws.amazon.com/new/, aws.amazon.com/blogs/machine-learning/, and GitHub awslabs via API).

## What needs to be built

The task is to write a `dependencies.py` (or similar) that constructs a real `WorkflowDependencies` instance and wire it into `main()` in `orchestrator.py`. Every field of `WorkflowDependencies` has a protocol defined — you just need concrete implementations:

- `memory` / `dedup_memory` / `topic_memory` → implement `MemoryProtocol` etc. with DynamoDB or AgentCore Memory
- `engagement_api` → implement `DevToAPIProtocol` with `requests` against dev.to REST API
- `engagement_memory` → DynamoDB
- `held_item_memory` → DynamoDB
- `ses_notifier` → boto3 SES
- `browser` / `screenshot_browser` → Playwright or AgentCore Browser
- `llm_scorer` / `llm_extractor` / `llm_formatter` / `llm_writer` → boto3 Bedrock `invoke_model`
- `s3_client` → boto3 S3
- `bundle_file_ops` / `gate_file_ops` → standard `pathlib` file operations

Then wire the trigger: Lambda handler → construct deps → call `run_workflow()` → EventBridge Scheduler fires weekly.

## Key files

- `magic_content_engine/orchestrator.py` — `run_workflow()` is the full 20-step pipeline; `main()` is the stub to fix
- `magic_content_engine/gateway.py` — `invoke_content_run_handler()` stub; `INVOKE_CONTENT_RUN_SCHEMA` is the correct payload schema
- `magic_content_engine/identity.py` — `LocalIdentityProvider` works locally; `AgentCoreIdentityProvider` is the production stub
- `magic_content_engine/models.py` — all dataclasses
- `magic_content_engine/config.py` — all env vars (S3_BUCKET, model IDs, thresholds, paths)
- `.kiro/steering/` — 6 markdown files loaded at runtime for voice rules and output templates
- `.env.example` — required environment variables

## AWS context

- Owner: Mike (AWS Community Builder, NZ)
- Region: ap-southeast-2 (Sydney) — AgentCore not available in ap-southeast-4 (Auckland)
- S3 bucket: `magic-content-dev`
- S3 key prefix: `output/` — **hardcoded in admin importer, do not change**
- Models: `anthropic.claude-haiku-4-5-20251001-v1:0` (structured tasks), `claude-sonnet-4-6` (narrative)
- Runs weekly, Monday 9am UTC

## Running locally

```bash
pip install -e ".[dev]"
cp .env.example .env  # fill in credentials
python -m magic_content_engine --source manual
```

Currently this exits immediately because `main()` is a stub. Fix `main()` first.

## Tests

Property-based tests use Hypothesis. Unit tests use pytest. Run with:
```bash
pytest magic_content_engine/
```

All tests use protocol stubs — no AWS credentials needed for testing.

## Agent skills

### Issue tracker

Issues live in GitHub Issues on `mikeartee/magic-content-engine` — use the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `tracking`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — one `CONTEXT.md` + `docs/adr/` at the repo root (neither exists yet; create when ready). See `docs/agents/domain.md`.

### Ship style

Pull request — feature branch → PR with `Closes #<issue>` → Mike merges. See `docs/agents/ship-style.md`.

### Project board

GitHub Projects v2 board "magic-content-engine" at https://github.com/users/mikeartee/projects/4. See `docs/agents/project-board.md`.
