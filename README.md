# Magic Content Engine

A scheduled, AWS-hosted agent application that automates weekly content research and generation. Built with the [Strands Agents SDK](https://github.com/strands-agents/sdk-python) (Python), deployed on [AgentCore Runtime](https://docs.aws.amazon.com/agentcore/), and orchestrated through two cooperating agents.

I built this content creation engine because as of 2026 I am an AWS Community Builder in the category "AI Engineering". I needed a way to track things that combined my knowledge of years of content creation on YouTube, and the main problem I found was this: It's difficult to keep up-to-date, even with all the best RSS feeds and sign-ups to press sites. Hopefully, we can help solve this problem, using this app, and another application that another AWS Builder created to drag things out of the Git Repos I create. Then we'll smoosh them together. 

This particular app is niched down on AI Engineering via AWS, but it can be easily modified for any type of search, just by changing the links in the `02-research-sources.md` steering file.

Building in public, and getting articles/videos written is going to be instrumental in my learning journey. 

## What it does

Every week (or on demand), the engine:

1. Crawls 9 research sources across Kiro IDE, AgentCore, Strands, Bedrock, and the wider AWS ecosystem
2. Deduplicates against previously covered articles stored in AgentCore Memory
3. Scores each article for relevance to the AI Engineering on AWS niche (1-5 scale via Claude Haiku)
4. Extracts metadata and builds APA 7th edition citations with BibTeX
5. Presents a Weekly Brief with topic coverage gaps and engagement signals from dev.to
6. Lets you pick which outputs to generate:
   - Blog post (dev.to Markdown with inline citations)
   - YouTube script + description
   - CFP proposal (25-min and 45-min variants)
   - User group session outline
   - Weekly digest email
7. Captures screenshots via AgentCore Browser (1440x900)
8. Assembles everything into a structured output bundle
9. Runs a Publish Gate review (approve / skip / hold with embargo / review)
10. Uploads approved files to S3

## Architecture

Two Strands agents handle the work:

- **Orchestrator_Agent** coordinates the full pipeline: crawling, scoring, citations, screenshots, bundle assembly, S3 upload
- **Writing_Sub_Agent** generates content outputs using steering files loaded at runtime from `.kiro/steering/`

Model routing is cost-optimised: Claude Haiku handles structured tasks (scoring, metadata, citations, digest), Claude Sonnet handles narrative writing (blog, script, CFP, user group).

### AWS services used

| Service | Role |
|---|---|
| AgentCore Runtime | Hosts both agents |
| AgentCore Browser | Crawls sources, captures screenshots |
| AgentCore Memory | Short-term session state + long-term persistence |
| AgentCore Gateway | Exposes `invoke_content_run` as MCP tool |
| AgentCore Identity | Manages API credentials in production |
| AgentCore Observability | Trace spans per workflow step |
| Amazon Bedrock | Claude Haiku + Claude Sonnet |
| Amazon S3 | Output bundle storage |
| Amazon SES | Embargo release notifications |
| Amazon EventBridge | Weekly cron trigger |

## Getting started

### Prerequisites

- Python 3.11+
- An AWS account with Bedrock model access (Claude Haiku, Claude Sonnet)
- [uv](https://docs.astral.sh/uv/) or pip for dependency management

### Install

```bash
git clone https://github.com/mikeartee/magic-content-engine.git
cd magic-content-engine
pip install -e ".[dev]"
```

### Configure

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

You need at minimum:
- `GITHUB_TOKEN` for GitHub API access
- `DEVTO_API_KEY` and `DEVTO_USERNAME` for engagement tracking
- `SES_SENDER_EMAIL` and `SES_RECIPIENT_EMAIL` for embargo notifications

The `.env` file is gitignored. In production, credentials are managed by AgentCore Identity.

### Run

```bash
# Manual run
python -m magic_content_engine --source manual

# With a specific date
python -m magic_content_engine --source manual --run-date 2026-03-16
```

### Test

```bash
pytest magic_content_engine/ -v
```

510 tests covering all modules. Uses [Hypothesis](https://hypothesis.readthedocs.io/) for property-based testing where applicable.

## Project structure

```
magic_content_engine/
  orchestrator.py      # Main workflow (20 steps)
  writing_agent.py     # Content generation (5 output types)
  crawler.py           # Source crawling (9 sources, retry logic)
  scoring.py           # Relevance scoring + engagement weighting
  metadata.py          # Metadata extraction with fallbacks
  citation.py          # APA 7th citations + BibTeX
  deduplication.py     # Article dedup against long-term memory
  topic_coverage.py    # Topic gap analysis + recommended focus
  engagement.py        # dev.to API integration
  weekly_brief.py      # Pre-run summary generation
  user_interaction.py  # Article confirmation + output selection
  publish_gate.py      # Approve / skip / hold / review workflow
  embargo.py           # Held item release checks + SES
  screenshots.py       # AgentCore Browser screenshot capture
  slug.py              # Kebab-case slug generation
  bundle.py            # Output directory assembly
  s3_upload.py         # S3 upload with exponential backoff
  memory.py            # Session + long-term memory (local JSON / AgentCore)
  identity.py          # Credential provider (local .env / AgentCore)
  gateway.py           # MCP tool registration via AgentCore Gateway
  observability.py     # Trace spans + error events
  models.py            # All dataclasses
  model_router.py      # Haiku/Sonnet task routing
  config.py            # Environment variable loading
  errors.py            # Error collection + retry logic
```

## Steering files

Voice rules and output templates live in `.kiro/steering/` and are loaded at runtime, not baked into agent prompts:

| File | Purpose |
|---|---|
| `01-niche-and-voice.md` | Voice rules, niche definition, placeholder format |
| `02-research-sources.md` | Primary and secondary source URLs |
| `03-output-blog-post.md` | Blog post structure template |
| `04-output-youtube.md` | YouTube script + description template |
| `05-output-talks.md` | CFP proposal + user group session template |
| `06-model-routing-and-bundle.md` | Model routing table + bundle structure |

## Output bundle

Each run produces a directory under `output/YYYY-MM-DD-[slug]/`:

```
output/2026-03-16-agentcore-memory-launch/
  post.md                    # if blog selected
  script.md                  # if YouTube selected
  description.txt            # if YouTube selected
  cfp-proposal.md            # if CFP selected
  usergroup-session.md       # if user group selected
  digest-email.txt           # if digest selected
  references.bib             # always (APA citations)
  cost-estimate.txt          # always (per-model token costs)
  agent-log.json             # always (full run metadata)
  screenshots/
    research/                # article landing pages
    console-runtime.png
    console-gateway.png
    console-memory.png
    console-observability.png
    sample-output.png
```


## Publish Gate

After content generation, each output goes through a review step:

- **Approve** adds the file to the S3 upload list
- **Skip** keeps it locally only
- **Hold** moves it to `output/held/` with an embargo release date
- **Review** moves it to `output/review/` for manual editing

Held items are checked at the start of each run. When a release date arrives, you get an SES notification and can choose to publish.

## Region note

AgentCore is not yet available in the Auckland region (ap-southeast-4). The closest supported region is Sydney (ap-southeast-2). This engine deploys to Sydney for now. When AgentCore lands in Auckland, that will be worth a content run of its own.

So, we'll be testing this out on my dev.to in the coming months to create content for our blog, and videos to come in the future. The main thrust of this project is to keep the human-in-the-loop. Keep an eye out for the blog posts on: https://dev.to/mikeartee

## Spec

The full requirements, design, and implementation plan live in `.kiro/specs/magic-content-engine/`. Built using Kiro's spec-driven development workflow.

## License

MIT — see [LICENSE](LICENSE) for details.
