# Infrastructure — mce-second-brain

All resources live in **ap-southeast-2** (AWS Asia Pacific — Sydney).
AgentCore is not yet available in ap-southeast-4 (Auckland), so Sydney
is the closest supported region for the bullpen pipeline.

Run `python scripts/create_infrastructure.py` to provision everything.

---

## S3 Bucket

| Property | Value |
|---|---|
| Bucket name | `mce-second-brain` |
| Region | `ap-southeast-2` |
| Versioning | Enabled |
| Public access | Blocked (all four settings) |

### Key prefixes

| Prefix | Purpose | Who reads | Who writes |
|---|---|---|---|
| `ami-context/` | Nightly vault sync feed — approved Obsidian notes synced from Mike's PC via `scripts/sync-vault-to-s3.ps1` every Sunday 10pm NZT | Researcher Agent | `sync-vault-to-s3.ps1` (via `aws s3 sync`) |
| `output/` | Published content bundles — **hardcoded in the admin importer, do not change this prefix** | Subeditor Agent, Publisher Agent | Writer Agent, Publisher Agent |
| `archive/` | Long-term knowledge archive maintained by the Archivist (Whakaaro) | Archivist Agent | Archivist Agent |

The `output/` prefix is hardcoded in the admin importer. Changing it
would break post discovery. The value is confirmed unchanged.

---

## DynamoDB Tables

All tables use **on-demand billing** (`PAY_PER_REQUEST`). No capacity
planning required.

### mce-checkpoints

Pipeline checkpoint and resume store. The Editor-in-Chief writes a
record here after each agent completes, enabling resumption from the
last successful step on failure.

| Attribute | Type | Key role |
|---|---|---|
| `run_id` | String | Partition key (HASH) |
| `agent_type` | String | Sort key (RANGE) |

Valid `agent_type` values: `researcher`, `desk_editor`, `writer`,
`subeditor`, `publisher`, `archivist`.

Example item:
```json
{
  "run_id": "2026-05-05-kiro-agentcore",
  "agent_type": "researcher",
  "status": "success",
  "completed_at": "2026-05-05T09:04:12Z",
  "output_hash": "sha256:abc123..."
}
```

### mce-topic-coverage

Tracks which topics have been covered in previous pipeline runs.
Used by the Desk Editor to avoid repeating angles.

| Attribute | Type | Key role |
|---|---|---|
| `topic` | String | Partition key (HASH) |

Example item:
```json
{
  "topic": "kiro-agentcore-integration",
  "first_covered": "2026-05-05",
  "run_count": 1
}
```

### mce-deduplication

Prevents the Researcher from surfacing articles that have already been
used in a previous content run.

| Attribute | Type | Key role |
|---|---|---|
| `article_url` | String | Partition key (HASH) |

Example item:
```json
{
  "article_url": "https://aws.amazon.com/blogs/machine-learning/...",
  "first_seen": "2026-05-05",
  "used_in_run": "2026-05-05-kiro-agentcore"
}
```

### mce-held-items

Content files held for manual review — either because the Subeditor
escalated after two revision cycles, or because Mike rejected at the
approval gate.

| Attribute | Type | Key role |
|---|---|---|
| `filename` | String | Partition key (HASH) |
| `run_date` | String | Sort key (RANGE) |

`run_date` is an ISO 8601 date string (`YYYY-MM-DD`).

Example item:
```json
{
  "filename": "post.md",
  "run_date": "2026-05-05",
  "reason": "max_revisions_reached",
  "run_id": "2026-05-05-kiro-agentcore"
}
```

---

## IAM Execution Roles (per-agent)

Each Lambda has a dedicated IAM role. Tool allowlists are enforced at
the IAM level — a Lambda without `s3:PutObject` literally cannot write
to S3 regardless of what the LLM requests.

| Agent | S3 GetObject | S3 PutObject | SES SendEmail | Bedrock |
|---|---|---|---|---|
| Researcher | `ami-context/*` only | — | — | Haiku |
| Desk Editor | — | — | — | Sonnet |
| Writer | — | `output/*` only | — | Sonnet + Haiku |
| Subeditor | `output/*` only | — | — | Sonnet |
| Publisher | `output/*` only | `output/*` only | ✓ | Haiku |
| Archivist | `ami-context/*` only | `archive/*` only | — | — |
| Editor-in-Chief | — | — | ✓ (approval gate) | — |

All roles include `logs:CreateLogGroup`, `logs:CreateLogStream`,
`logs:PutLogEvents` for CloudWatch.

The Editor-in-Chief also needs:
- `lambda:InvokeFunction` on all `mce-*` functions
- `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `Query` on
  `mce-checkpoints` and `mce-held-items`

---

## EventBridge Scheduler Rules

Both rules live in **ap-southeast-2** and use `FLEXIBLE_TIME_WINDOW Mode=OFF`,
meaning they fire at the exact scheduled time with no flexibility window.

`create_eventbridge_schedules()` in `scripts/create_infrastructure.py`
creates (or updates) both rules via boto3. It resolves the AWS account ID
from STS automatically, so no manual ARN construction is needed.

### mce-editor-in-chief-weekly

Triggers the Editor-in-Chief Lambda every Monday morning NZT so the
content pipeline starts at the beginning of the working week.

| Property | Value |
|---|---|
| Schedule name | `mce-editor-in-chief-weekly` |
| Schedule expression | `cron(0 9 ? * MON *)` |
| Timezone | `Pacific/Auckland` |
| Flexible time window | `OFF` |
| Target Lambda | `mce-editor-in-chief` |
| Target ARN format | `arn:aws:lambda:ap-southeast-2:{account_id}:function:mce-editor-in-chief` |
| Scheduler role | `arn:aws:iam::{account_id}:role/eventbridge-scheduler-role` |
| Input payload | `{"source": "scheduled"}` |

The cron expression `cron(0 9 ? * MON *)` breaks down as:
- `0` — minute 0
- `9` — hour 9
- `?` — any day-of-month (required placeholder when day-of-week is set)
- `*` — every month
- `MON` — Monday only
- `*` — every year

### mce-archivist-nightly

Triggers the Archivist (Whakaaro) Lambda every night at 11pm NZT,
independent of the main content pipeline.

| Property | Value |
|---|---|
| Schedule name | `mce-archivist-nightly` |
| Schedule expression | `cron(0 23 * * ? *)` |
| Timezone | `Pacific/Auckland` |
| Flexible time window | `OFF` |
| Target Lambda | `mce-archivist` |
| Target ARN format | `arn:aws:lambda:ap-southeast-2:{account_id}:function:mce-archivist` |
| Scheduler role | `arn:aws:iam::{account_id}:role/eventbridge-scheduler-role` |
| Input payload | `{"source": "scheduled"}` |

The cron expression `cron(0 23 * * ? *)` breaks down as:
- `0` — minute 0
- `23` — hour 23 (11pm)
- `*` — every day-of-month
- `*` — every month
- `?` — any day-of-week (required placeholder when day-of-month is set)
- `*` — every year

### Scheduler IAM role

The `eventbridge-scheduler-role` needs `lambda:InvokeFunction` on both
target Lambdas. Create it manually in IAM before running the provisioning
script, or the `create_eventbridge_schedules()` call will fail with an
`InvalidClientTokenId` or `AccessDenied` error.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": [
        "arn:aws:lambda:ap-southeast-2:*:function:mce-editor-in-chief",
        "arn:aws:lambda:ap-southeast-2:*:function:mce-archivist"
      ]
    }
  ]
}
```

The trust policy must allow `scheduler.amazonaws.com` to assume the role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "scheduler.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

---

## Environment Variables

See `magic_content_engine/config.py` for the full list. Key variables
added for the bullpen architecture:

| Variable | Default | Description |
|---|---|---|
| `MCE_SECOND_BRAIN_BUCKET` | `mce-second-brain` | S3 bucket name |
| `MCE_CHECKPOINTS_TABLE` | `mce-checkpoints` | DynamoDB checkpoint table |
| `MCE_TOPIC_COVERAGE_TABLE` | `mce-topic-coverage` | DynamoDB topic coverage table |
| `MCE_DEDUPLICATION_TABLE` | `mce-deduplication` | DynamoDB deduplication table |
| `MCE_HELD_ITEMS_TABLE` | `mce-held-items` | DynamoDB held items table |

---

## Provisioning

```bash
# Requires: AWS credentials configured for ap-southeast-2
pip install boto3
python scripts/create_infrastructure.py
```

The script is idempotent — re-running it skips resources that already
exist. It creates the S3 bucket, all four DynamoDB tables, and both
EventBridge Scheduler rules, then prints instructions for Secrets
Manager and Lambda deployment.

The EventBridge step resolves the AWS account ID from STS automatically.
It requires the `eventbridge-scheduler-role` IAM role to exist before
running — see the EventBridge Scheduler Rules section above for the
required trust policy and permissions.
