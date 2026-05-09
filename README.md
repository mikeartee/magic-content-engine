# Magic Content Engine

A weekly content research and generation pipeline for an AWS Community Builder. It crawls AWS and Kiro IDE sources, scores articles by relevance, generates blog posts and other content outputs through a constrained multi-agent bullpen, and presents a review step before anything gets published.

Built with Python 3.13, [Strands Agents SDK](https://github.com/strands-agents/sdk-python), AWS Lambda Durable Functions, Amazon Bedrock, DynamoDB, and SES. Runs locally via `scripts/run_local.py` or automatically on a Monday 9am NZT schedule via EventBridge Scheduler.

## Why this exists

Keeping up with the AWS AI Engineering ecosystem from Aotearoa New Zealand is genuinely hard. Re:Invent happens while NZ is asleep. Kiro changelogs drop without warning. Strands SDK updates land in GitHub before they hit any blog. This engine crawls the sources I care about, scores what's relevant, and drafts content so I can focus on the parts only I can write.

## Architecture: the bullpen

Six constrained agents handle the pipeline. Each has an IAM execution role that enforces what it can and cannot do at the infrastructure level — not just the prompt level.

| Agent | Role | IAM constraints |
|---|---|---|
| **Researcher** | Crawls 8 sources + reads vault from S3 | S3 GetObject (ami-context/ only), Bedrock Haiku. No S3 PutObject, no SES. |
| **Desk Editor** | Research brief → content brief | Bedrock Sonnet only. No S3, no SES. |
| **Writer** | Content brief → output bundle | S3 PutObject (output/ only), Bedrock Sonnet + Haiku. No SES. |
| **Subeditor** | Reviews content, returns publish/revise/spike verdict | S3 GetObject (output/ only), Bedrock Sonnet. No writes. |
| **Publisher** | Uploads approved files to S3, sends SES notification | S3 PutObject (output/ only), SES SendEmail. No S3 DeleteObject. |
| **Archivist (Whakaaro)** | Nightly vault summarisation | S3 GetObject (ami-context/), S3 PutObject (archive/). No SES. |

The **Editor-in-Chief** orchestrates the pipeline as a Lambda Durable Function using `step()` for each agent and `wait()` for the approval gate. It pauses indefinitely at zero compute cost until you approve or reject via email link.

The revision loop: when the Subeditor returns `revise`, the Writer gets another pass with the feedback. Maximum 2 cycles per file before escalation to manual review.

## Research sources

8 sources crawled via HTTP GET, RSS, and GitHub API — no browser required:

1. kiro.dev/changelog/ide/
2. kiro.dev/changelog/cli/
3. github.com/kirodotdev/Kiro/issues (GitHub API)
4. aws.amazon.com/new/ (keyword filter: bedrock, agentcore, kiro, lambda)
5. aws.amazon.com/blogs/machine-learning/
6. community.aws (RSS)
7. github.com/awslabs/ (GitHub API, new releases)
8. strandsagents.com

## Vault context

An Obsidian vault on my PC is synced to `s3://mce-second-brain/ami-context/` every Sunday at 10pm NZT via Windows Task Scheduler. The Researcher reads from this alongside the external sources, so generated content reflects what I've actually been building — not just what AWS announced.

## Running locally

### Prerequisites

- Python 3.13
- AWS credentials configured for ap-southeast-2
- `.env` file populated (copy `.env.example` and fill in real values)

### Install

```bash
git clone https://github.com/mikeartee/magic-content-engine.git
cd magic-content-engine
pip install -e ".[dev]"
```

### Configure

```bash
cp .env.example .env
# Fill in: APPROVAL_TOKEN_SECRET, SES_SENDER_EMAIL, SES_RECIPIENT_EMAIL,
#          GITHUB_TOKEN, VAULT_PATH, and the MCE_* DynamoDB/S3 variables
```

Generate your approval token secret:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### Provision infrastructure (once)

```bash
python scripts/create_infrastructure.py
```

Creates the `mce-second-brain` S3 bucket and all DynamoDB tables in ap-southeast-2.

### Run

```bash
# Dry run (stub article, no real crawl — good for testing the pipeline)
python scripts/run_local.py --topic "Kiro IDE latest features" --outputs blog --dry-run

# Real run (crawls all 8 sources, takes 2-3 minutes)
python scripts/run_local.py --topic "Kiro IDE latest features" --outputs blog

# Multiple outputs
python scripts/run_local.py --topic "AgentCore GA" --outputs blog youtube

# All outputs
python scripts/run_local.py --topic "Strands SDK update" --outputs all
```

The pipeline pauses at the approval gate and prints a verdict summary. Type `y` to publish to S3 or `N` to keep files local.

### Test

```bash
pytest magic_content_engine/ -v
```

Property-based tests use [Hypothesis](https://hypothesis.readthedocs.io/).

## Vault sync (Windows Task Scheduler)

The vault sync runs automatically every Sunday at 10pm NZT. Setup instructions are in `scripts/README.md`. The task is already configured — run `schtasks /query /tn "MCE Vault Sync"` to verify.

## Output bundle

Each run produces a directory under `output/YYYY-MM-DD-[slug]/`:

```
output/2026-05-10-kiro-ide-latest-features/
  post.md                    # blog post (if selected)
  script.md                  # YouTube script (if selected)
  description.txt            # YouTube description (if selected)
  cfp-proposal.md            # CFP proposal (if selected)
  usergroup-session.md       # user group session (if selected)
  digest-email.txt           # digest email (if selected)
  references.bib             # APA citations (always)
  cost-estimate.txt          # per-model token costs (always)
  agent-log.jsonl            # structured decision log (always)
  checkpoints.json           # pipeline checkpoints (always)
  screenshots/               # placeholder — real screenshots in future
```

## S3 key format

Files upload to `s3://mce-second-brain/output/YYYY-MM-DD-[slug]/[filename]`.

The `output/` prefix is hardcoded in the admin importer and must not change.

## Steering files

Voice rules and output templates live in `.kiro/steering/` and are loaded at runtime:

| File | Purpose |
|---|---|
| `01-niche-and-voice.md` | Voice rules, niche definition, placeholder format |
| `02-research-sources.md` | Primary and secondary source URLs |
| `03-output-blog-post.md` | Blog post structure template |
| `04-output-youtube.md` | YouTube script + description template |
| `05-output-talks.md` | CFP proposal + user group session template |
| `06-model-routing-and-bundle.md` | Model routing table + bundle structure |

## Model routing

| Task | Model |
|---|---|
| Relevance scoring, metadata extraction | Claude Haiku (`au.anthropic.claude-haiku-4-5-20251001-v1:0`) |
| Editorial decisions, quality review | Claude Sonnet (`au.anthropic.claude-sonnet-4-6`) |
| Blog post, YouTube script, CFP, user group | Claude Sonnet |
| Digest email, Publisher email formatting | Claude Haiku |

Uses `au.` inference profile prefix for ap-southeast-2 cross-region routing.

## AWS services

| Service | Role |
|---|---|
| Amazon Bedrock | Claude Haiku + Claude Sonnet via `au.` inference profiles |
| Amazon S3 (`mce-second-brain`) | Vault context feed, output bundles, archive |
| Amazon DynamoDB | Checkpoints, run history (AMI_Log), topic coverage, deduplication, held items |
| Amazon SES | Approval gate email, publication notification |
| EventBridge Scheduler | Monday 9am NZT trigger (`Pacific/Auckland` timezone) |
| AWS Lambda | Each agent runs as a separate Lambda with its own IAM execution role |

## Region note

AgentCore is not yet available in the Auckland region (ap-southeast-4). The closest supported region is Sydney (ap-southeast-2). All Lambda functions, DynamoDB tables, and S3 buckets are in ap-southeast-2. When AgentCore lands in Auckland, that will be worth a content run of its own.

## Spec

Full requirements, design, and implementation plan live in `.kiro/specs/bullpen-architecture/`. Built using Kiro's spec-driven development workflow.

## What's next

- **Issue #18**: Local web GUI — topic idea suggestions, pipeline runner, inline editor, one-click publish to dev.to

## License

MIT
