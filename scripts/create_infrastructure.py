#!/usr/bin/env python3
"""Create AWS infrastructure for the Magic Content Engine (mce-second-brain).

Run once before first deployment:
  python scripts/create_infrastructure.py

What this script does:
  1. Creates the S3 bucket mce-second-brain with versioning enabled
  2. Creates DynamoDB table mce-checkpoints (run_id + agent_type)
  3. Creates DynamoDB table mce-topic-coverage (topic)
  4. Creates DynamoDB table mce-deduplication (article_url)
  5. Creates DynamoDB table mce-held-items (filename + run_date)
  6. Prints Secrets Manager instructions
  7. Prints Lambda deployment instructions
  8. Prints EventBridge Scheduler instructions

All DynamoDB tables use on-demand billing (PAY_PER_REQUEST).

Prerequisites:
  - AWS CLI configured with ap-southeast-2 credentials
  - pip install boto3
"""
from __future__ import annotations

import json
import sys

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"

# --- S3 ---
S3_BUCKET = "mce-second-brain"

# S3 key prefixes (documented here; the bucket itself has no prefix enforcement)
S3_KEY_PREFIXES = {
    "ami_context": "ami-context/",   # nightly vault sync feed — Researcher reads from here
    "output": "output/",             # published content — hardcoded in admin importer, do not change
    "archive": "archive/",           # Archivist (Whakaaro) long-term knowledge archive
}

# --- DynamoDB tables ---
# mce-checkpoints: pipeline checkpoint/resume per run
CHECKPOINTS_TABLE = "mce-checkpoints"

# mce-topic-coverage: tracks which topics have been covered
TOPIC_COVERAGE_TABLE = "mce-topic-coverage"

# mce-deduplication: prevents re-publishing the same article URL
DEDUPLICATION_TABLE = "mce-deduplication"

# mce-held-items: content held for manual review
HELD_ITEMS_TABLE = "mce-held-items"

# --- Legacy (original single-agent pipeline) ---
LEGACY_TABLE = "magic-content-engine"
SECRET_NAME = "magic-content-engine/credentials"
LAMBDA_FUNCTION = "magic-content-engine"


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

def create_s3_bucket() -> None:
    print(f"\n[1/6] Creating S3 bucket: {S3_BUCKET} in {REGION}")
    client = boto3.client("s3", region_name=REGION)

    try:
        client.head_bucket(Bucket=S3_BUCKET)
        print(f"  ✓ Bucket already exists — skipping creation")
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("404", "NoSuchBucket"):
            client.create_bucket(
                Bucket=S3_BUCKET,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
            print(f"  ✓ Bucket created: s3://{S3_BUCKET}")
        elif error_code == "403":
            print(
                f"  ✗ Bucket {S3_BUCKET!r} exists but is owned by another account. "
                "Choose a different bucket name.",
                file=sys.stderr,
            )
            raise
        else:
            raise

    # Enable versioning
    client.put_bucket_versioning(
        Bucket=S3_BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )
    print(f"  ✓ Versioning enabled on s3://{S3_BUCKET}")

    # Block all public access
    client.put_public_access_block(
        Bucket=S3_BUCKET,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    print(f"  ✓ Public access blocked on s3://{S3_BUCKET}")

    print(f"\n  Key prefixes (logical — no physical folders required):")
    for name, prefix in S3_KEY_PREFIXES.items():
        print(f"    s3://{S3_BUCKET}/{prefix}  ({name})")


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------

def _create_table(
    client: "boto3.client",
    table_name: str,
    attribute_definitions: list[dict],
    key_schema: list[dict],
    step_label: str,
) -> None:
    print(f"\n[{step_label}] Creating DynamoDB table: {table_name}")

    try:
        client.describe_table(TableName=table_name)
        print(f"  ✓ Table already exists — skipping")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    client.create_table(
        TableName=table_name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=attribute_definitions,
        KeySchema=key_schema,
    )

    waiter = client.get_waiter("table_exists")
    print(f"  Waiting for table to become active...")
    waiter.wait(TableName=table_name)
    print(f"  ✓ Table created: {table_name}")


# ---------------------------------------------------------------------------
# DynamoDB tables
# ---------------------------------------------------------------------------

def create_checkpoints_table(client: "boto3.client") -> None:
    """mce-checkpoints — pipeline checkpoint/resume.

    Partition key : run_id     (S) — unique identifier for a pipeline run
    Sort key      : agent_type (S) — researcher | desk_editor | writer | subeditor | publisher | archivist
    """
    _create_table(
        client=client,
        table_name=CHECKPOINTS_TABLE,
        attribute_definitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "agent_type", "AttributeType": "S"},
        ],
        key_schema=[
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "agent_type", "KeyType": "RANGE"},
        ],
        step_label="2/6",
    )


def create_topic_coverage_table(client: "boto3.client") -> None:
    """mce-topic-coverage — tracks which topics have been covered.

    Partition key : topic (S) — the topic string from BullpenBrief
    """
    _create_table(
        client=client,
        table_name=TOPIC_COVERAGE_TABLE,
        attribute_definitions=[
            {"AttributeName": "topic", "AttributeType": "S"},
        ],
        key_schema=[
            {"AttributeName": "topic", "KeyType": "HASH"},
        ],
        step_label="3/6",
    )


def create_deduplication_table(client: "boto3.client") -> None:
    """mce-deduplication — prevents re-publishing the same article URL.

    Partition key : article_url (S) — the canonical URL of the article
    """
    _create_table(
        client=client,
        table_name=DEDUPLICATION_TABLE,
        attribute_definitions=[
            {"AttributeName": "article_url", "AttributeType": "S"},
        ],
        key_schema=[
            {"AttributeName": "article_url", "KeyType": "HASH"},
        ],
        step_label="4/6",
    )


def create_held_items_table(client: "boto3.client") -> None:
    """mce-held-items — content held for manual review.

    Partition key : filename (S)  — the content filename
    Sort key      : run_date (S)  — ISO 8601 date of the pipeline run
    """
    _create_table(
        client=client,
        table_name=HELD_ITEMS_TABLE,
        attribute_definitions=[
            {"AttributeName": "filename", "AttributeType": "S"},
            {"AttributeName": "run_date", "AttributeType": "S"},
        ],
        key_schema=[
            {"AttributeName": "filename", "KeyType": "HASH"},
            {"AttributeName": "run_date", "KeyType": "RANGE"},
        ],
        step_label="5/6",
    )


# ---------------------------------------------------------------------------
# Instructions (printed, not executed)
# ---------------------------------------------------------------------------

def print_secret_instructions() -> None:
    print(f"\n[6/6] Create Secrets Manager secret (do this manually in the console or CLI)")
    secret_value = {
        "github_token": "ghp_YOUR_GITHUB_TOKEN_HERE",
        "devto_api_key": "YOUR_DEVTO_API_KEY_HERE",
        "devto_username": "YOUR_DEVTO_USERNAME_HERE",
        "ses_sender_email": "YOUR_VERIFIED_SES_SENDER@example.com",
        "ses_recipient_email": "YOUR_RECIPIENT@example.com",
    }
    print(f"""
  Run this AWS CLI command (fill in real values first):

  aws secretsmanager create-secret \\
    --name {SECRET_NAME} \\
    --region {REGION} \\
    --secret-string '{json.dumps(secret_value)}'

  Or update an existing secret:

  aws secretsmanager update-secret \\
    --secret-id {SECRET_NAME} \\
    --region {REGION} \\
    --secret-string '{json.dumps(secret_value)}'

  Note: SES sender email must be verified in SES before use.
""")


def print_lambda_instructions() -> None:
    print("""
  ── Lambda deployment ──────────────────────────────────────────────────────

  Build and deploy:

  pip install -r requirements-lambda.txt -t ./package/ --quiet
  cp -r magic_content_engine ./package/
  cp lambda_handler.py ./package/
  cp -r .kiro ./package/
  cd package && zip -r ../magic-content-engine.zip . && cd ..

  # Create (first time)
  aws lambda create-function \\
    --function-name {fn} \\
    --runtime python3.13 \\
    --handler lambda_handler.handler \\
    --timeout 900 \\
    --memory-size 1024 \\
    --zip-file fileb://magic-content-engine.zip \\
    --role arn:aws:iam::YOUR_ACCOUNT_ID:role/magic-content-engine-role \\
    --region {region} \\
    --environment "Variables={{\\
      MCE_SECOND_BRAIN_BUCKET={bucket},\\
      MCE_CHECKPOINTS_TABLE={chk},\\
      MCE_TOPIC_COVERAGE_TABLE={cov},\\
      MCE_DEDUPLICATION_TABLE={ded},\\
      MCE_HELD_ITEMS_TABLE={held},\\
      SECRETS_NAME={secret},\\
      STEERING_BASE_PATH=/var/task/.kiro/steering/,\\
      LOG_LEVEL=INFO\\
    }}"

  # Update code (subsequent deploys)
  aws lambda update-function-code \\
    --function-name {fn} \\
    --zip-file fileb://magic-content-engine.zip \\
    --region {region}

  Required IAM permissions for the Lambda execution role:
    - bedrock:InvokeModel
    - dynamodb:GetItem, PutItem, UpdateItem, DeleteItem, Query
        on tables: {chk}, {cov}, {ded}, {held}
    - secretsmanager:GetSecretValue (secret: {secret})
    - s3:GetObject  (bucket: {bucket}, prefix: ami-context/*)
    - s3:PutObject  (bucket: {bucket}, prefix: output/*)
    - s3:PutObject  (bucket: {bucket}, prefix: archive/*)
    - ses:SendEmail (approval gate only)
    - logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents
""".format(
        fn=LAMBDA_FUNCTION,
        region=REGION,
        bucket=S3_BUCKET,
        chk=CHECKPOINTS_TABLE,
        cov=TOPIC_COVERAGE_TABLE,
        ded=DEDUPLICATION_TABLE,
        held=HELD_ITEMS_TABLE,
        secret=SECRET_NAME,
    ))


def print_eventbridge_instructions() -> None:
    print("""
  ── EventBridge Scheduler ──────────────────────────────────────────────────

  Editor-in-Chief — Monday 9am NZT (Pacific/Auckland):

  aws scheduler create-schedule \\
    --name mce-editor-in-chief-weekly \\
    --schedule-expression "cron(0 9 ? * MON *)" \\
    --schedule-expression-timezone "Pacific/Auckland" \\
    --flexible-time-window "Mode=OFF" \\
    --target '{
      "Arn": "arn:aws:lambda:{region}:YOUR_ACCOUNT_ID:function:{fn}",
      "RoleArn": "arn:aws:iam::YOUR_ACCOUNT_ID:role/eventbridge-scheduler-role",
      "Input": "{\\"source\\": \\"scheduled\\"}"
    }' \\
    --region {region}

  Archivist (Whakaaro) — nightly 11pm NZT:

  aws scheduler create-schedule \\
    --name mce-archivist-nightly \\
    --schedule-expression "cron(0 23 * * ? *)" \\
    --schedule-expression-timezone "Pacific/Auckland" \\
    --flexible-time-window "Mode=OFF" \\
    --target '{
      "Arn": "arn:aws:lambda:{region}:YOUR_ACCOUNT_ID:function:mce-archivist",
      "RoleArn": "arn:aws:iam::YOUR_ACCOUNT_ID:role/eventbridge-scheduler-role",
      "Input": "{\\"source\\": \\"scheduled\\"}"
    }' \\
    --region {region}

  The scheduler role needs:
    - lambda:InvokeFunction on both Lambda functions

  Manual test invoke:
  aws lambda invoke \\
    --function-name {fn} \\
    --payload '{{"source": "manual"}}' \\
    --region {region} \\
    response.json && cat response.json
""".format(region=REGION, fn=LAMBDA_FUNCTION))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Magic Content Engine — mce-second-brain Infrastructure Setup")
    print("=" * 60)
    print(f"Region : {REGION}")
    print(f"Bucket : s3://{S3_BUCKET}")
    print(f"Tables : {CHECKPOINTS_TABLE}, {TOPIC_COVERAGE_TABLE}, "
          f"{DEDUPLICATION_TABLE}, {HELD_ITEMS_TABLE}")

    # S3
    try:
        create_s3_bucket()
    except Exception as exc:
        print(f"  ✗ S3 bucket creation failed: {exc}", file=sys.stderr)
        print("  Check your AWS credentials and try again.", file=sys.stderr)
        sys.exit(1)

    # DynamoDB
    ddb = boto3.client("dynamodb", region_name=REGION)
    errors: list[str] = []

    for fn in (
        create_checkpoints_table,
        create_topic_coverage_table,
        create_deduplication_table,
        create_held_items_table,
    ):
        try:
            fn(ddb)
        except Exception as exc:
            msg = f"  ✗ {fn.__name__} failed: {exc}"
            print(msg, file=sys.stderr)
            errors.append(msg)

    # Printed instructions
    print_secret_instructions()
    print_lambda_instructions()
    print_eventbridge_instructions()

    print("=" * 60)
    if errors:
        print(f"Completed with {len(errors)} error(s). See above for details.")
        sys.exit(1)
    else:
        print("Done. All resources created (or already existed).")
        print("Follow the printed instructions to complete Lambda and scheduler setup.")


if __name__ == "__main__":
    main()
