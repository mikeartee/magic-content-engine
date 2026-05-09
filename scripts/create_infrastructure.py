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
  6. Creates IAM execution roles for each Lambda (least-privilege per agent)
  7. Prints Secrets Manager instructions
  8. Prints Lambda deployment instructions
  9. Prints EventBridge Scheduler instructions

All DynamoDB tables use on-demand billing (PAY_PER_REQUEST).

Prerequisites:
  - AWS CLI configured with ap-southeast-2 credentials
  - pip install boto3
"""
from __future__ import annotations

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"

# --- S3 ---
S3_BUCKET = "mce-second-brain"

# S3 key prefixes (documented here; the bucket itself has no prefix enforcement)
S3_KEY_PREFIXES = {
    "ami_context": "ami-context/",   # nightly vault sync feed ΓÇö Researcher reads from here
    "output": "output/",             # published content ΓÇö hardcoded in admin importer, do not change
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

# --- IAM roles (per-agent, least-privilege) ---
# Policy documents live in docs/iam-policies/ relative to the repo root.
IAM_ROLES: dict[str, dict] = {
    "mce-researcher-role": {
        "description": "Researcher Lambda ΓÇö read-only S3 ami-context, Bedrock Haiku, CloudWatch Logs",
        "policy_file": "researcher-policy.json",
    },
    "mce-desk-editor-role": {
        "description": "Desk Editor Lambda ΓÇö Bedrock Sonnet only, CloudWatch Logs",
        "policy_file": "desk-editor-policy.json",
    },
    "mce-writer-role": {
        "description": "Writer Lambda ΓÇö S3 PutObject output/* only, Bedrock Sonnet+Haiku, CloudWatch Logs",
        "policy_file": "writer-policy.json",
    },
    "mce-subeditor-role": {
        "description": "Subeditor Lambda ΓÇö S3 GetObject output/* only, Bedrock Sonnet, CloudWatch Logs",
        "policy_file": "subeditor-policy.json",
    },
    "mce-publisher-role": {
        "description": "Publisher Lambda ΓÇö S3 GetObject+PutObject output/*, SES SendEmail, CloudWatch Logs",
        "policy_file": "publisher-policy.json",
    },
    "mce-archivist-role": {
        "description": "Archivist Lambda ΓÇö S3 GetObject ami-context/*, S3 PutObject archive/*, CloudWatch Logs",
        "policy_file": "archivist-policy.json",
    },
    "mce-editor-in-chief-role": {
        "description": "Editor-in-Chief Lambda ΓÇö Lambda InvokeFunction mce-*, DynamoDB checkpoints+run-history, SES, CloudWatch Logs",
        "policy_file": "editor-in-chief-policy.json",
    },
}

# Trust policy allowing Lambda to assume these roles
_LAMBDA_TRUST_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
})

# --- EventBridge Scheduler ---
# Both schedules use Pacific/Auckland timezone and FLEXIBLE_TIME_WINDOW OFF.
SCHEDULE_EDITOR_IN_CHIEF = "mce-editor-in-chief-weekly"
SCHEDULE_ARCHIVIST = "mce-archivist-nightly"

# cron(0 9 ? * MON *) — Monday 9am NZT
SCHEDULE_EXPR_EDITOR_IN_CHIEF = "cron(0 9 ? * MON *)"

# cron(0 23 * * ? *) — nightly 11pm NZT
SCHEDULE_EXPR_ARCHIVIST = "cron(0 23 * * ? *)"

SCHEDULE_TIMEZONE = "Pacific/Auckland"

# Lambda function names targeted by the schedules
LAMBDA_EDITOR_IN_CHIEF = "mce-editor-in-chief"
LAMBDA_ARCHIVIST = "mce-archivist"


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

def create_s3_bucket() -> None:
    print(f"\n[1/6] Creating S3 bucket: {S3_BUCKET} in {REGION}")
    client = boto3.client("s3", region_name=REGION)

    try:
        client.head_bucket(Bucket=S3_BUCKET)
        print(f"  Γ£ô Bucket already exists ΓÇö skipping creation")
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("404", "NoSuchBucket"):
            client.create_bucket(
                Bucket=S3_BUCKET,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
            print(f"  Γ£ô Bucket created: s3://{S3_BUCKET}")
        elif error_code == "403":
            print(
                f"  Γ£ù Bucket {S3_BUCKET!r} exists but is owned by another account. "
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
    print(f"  Γ£ô Versioning enabled on s3://{S3_BUCKET}")

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
    print(f"  Γ£ô Public access blocked on s3://{S3_BUCKET}")

    print(f"\n  Key prefixes (logical ΓÇö no physical folders required):")
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
        print(f"  Γ£ô Table already exists ΓÇö skipping")
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
    print(f"  Γ£ô Table created: {table_name}")


# ---------------------------------------------------------------------------
# DynamoDB tables
# ---------------------------------------------------------------------------

def create_checkpoints_table(client: "boto3.client") -> None:
    """mce-checkpoints ΓÇö pipeline checkpoint/resume.

    Partition key : run_id     (S) ΓÇö unique identifier for a pipeline run
    Sort key      : agent_type (S) ΓÇö researcher | desk_editor | writer | subeditor | publisher | archivist
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
        step_label="2/9",
    )


def create_topic_coverage_table(client: "boto3.client") -> None:
    """mce-topic-coverage ΓÇö tracks which topics have been covered.

    Partition key : topic (S) ΓÇö the topic string from BullpenBrief
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
        step_label="3/9",
    )


def create_deduplication_table(client: "boto3.client") -> None:
    """mce-deduplication ΓÇö prevents re-publishing the same article URL.

    Partition key : article_url (S) ΓÇö the canonical URL of the article
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
        step_label="4/9",
    )


def create_held_items_table(client: "boto3.client") -> None:
    """mce-held-items ΓÇö content held for manual review.

    Partition key : filename (S)  ΓÇö the content filename
    Sort key      : run_date (S)  ΓÇö ISO 8601 date of the pipeline run
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
        step_label="5/9",
    )


# ---------------------------------------------------------------------------
# IAM execution roles (per-agent, least-privilege)
# ---------------------------------------------------------------------------

def create_iam_roles(policy_dir: str | None = None) -> list[str]:
    """Create per-agent IAM execution roles and attach inline policies.

    Each Lambda agent gets a dedicated role with only the permissions it needs.
    Roles are idempotent ΓÇö re-running skips roles that already exist.

    Args:
        policy_dir: Path to the directory containing policy JSON files.
                    Defaults to ``docs/iam-policies/`` relative to the repo root
                    (i.e. the directory two levels above this script).

    Returns:
        List of error messages encountered (empty on full success).
    """
    if policy_dir is None:
        # scripts/ ΓåÆ repo root ΓåÆ docs/iam-policies/
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        policy_dir = os.path.join(repo_root, "docs", "iam-policies")

    print(f"\n[6/9] Creating IAM execution roles (least-privilege per Lambda)")
    print(f"  Policy directory: {policy_dir}")

    iam = boto3.client("iam", region_name=REGION)
    errors: list[str] = []

    for role_name, role_cfg in IAM_ROLES.items():
        policy_path = os.path.join(policy_dir, role_cfg["policy_file"])

        # Load policy document
        try:
            with open(policy_path, encoding="utf-8") as fh:
                policy_document = fh.read()
            # Validate it parses as JSON
            json.loads(policy_document)
        except FileNotFoundError:
            msg = f"  Γ£ù Policy file not found: {policy_path}"
            print(msg, file=sys.stderr)
            errors.append(msg)
            continue
        except json.JSONDecodeError as exc:
            msg = f"  Γ£ù Invalid JSON in {policy_path}: {exc}"
            print(msg, file=sys.stderr)
            errors.append(msg)
            continue

        # Create role (or confirm it exists)
        try:
            iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=_LAMBDA_TRUST_POLICY,
                Description=role_cfg["description"],
                Tags=[
                    {"Key": "Project", "Value": "magic-content-engine"},
                    {"Key": "ManagedBy", "Value": "create_infrastructure.py"},
                ],
            )
            print(f"  Γ£ô Role created: {role_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "EntityAlreadyExists":
                print(f"  Γ£ô Role already exists ΓÇö skipping creation: {role_name}")
            else:
                msg = f"  Γ£ù Failed to create role {role_name}: {e}"
                print(msg, file=sys.stderr)
                errors.append(msg)
                continue

        # Attach (or update) the inline policy
        inline_policy_name = f"{role_name}-policy"
        try:
            iam.put_role_policy(
                RoleName=role_name,
                PolicyName=inline_policy_name,
                PolicyDocument=policy_document,
            )
            print(f"    Γ£ô Inline policy attached: {inline_policy_name}")
        except ClientError as e:
            msg = f"  Γ£ù Failed to attach policy to {role_name}: {e}"
            print(msg, file=sys.stderr)
            errors.append(msg)

    if not errors:
        print(f"\n  All {len(IAM_ROLES)} IAM roles created/verified.")
    else:
        print(f"\n  Completed with {len(errors)} IAM error(s). See above for details.")

    return errors


# ---------------------------------------------------------------------------
# EventBridge Scheduler
# ---------------------------------------------------------------------------


def create_eventbridge_schedules(account_id: str, scheduler_role_arn: str) -> None:
    """Create EventBridge Scheduler rules for the Editor-in-Chief and Archivist.

    Both rules:
    - Use Pacific/Auckland timezone
    - Use FLEXIBLE_TIME_WINDOW Mode=OFF (fire at the exact scheduled time)
    - Target the respective Lambda function ARN
    """
    print(f"\n[6/9] Creating EventBridge Scheduler rules")
    client = boto3.client("scheduler", region_name=REGION)

    schedules = [
        {
            "name": SCHEDULE_EDITOR_IN_CHIEF,
            "expression": SCHEDULE_EXPR_EDITOR_IN_CHIEF,
            "lambda_name": LAMBDA_EDITOR_IN_CHIEF,
            "description": "Editor-in-Chief weekly pipeline — Monday 9am NZT",
        },
        {
            "name": SCHEDULE_ARCHIVIST,
            "expression": SCHEDULE_EXPR_ARCHIVIST,
            "lambda_name": LAMBDA_ARCHIVIST,
            "description": "Archivist (Whakaaro) nightly archive — 11pm NZT",
        },
    ]

    for sched in schedules:
        lambda_arn = (
            f"arn:aws:lambda:{REGION}:{account_id}:function:{sched['lambda_name']}"
        )
        _create_or_update_schedule(
            client=client,
            name=sched["name"],
            schedule_expression=sched["expression"],
            timezone=SCHEDULE_TIMEZONE,
            target_arn=lambda_arn,
            scheduler_role_arn=scheduler_role_arn,
            description=sched["description"],
        )


def _create_or_update_schedule(
    client: "boto3.client",
    name: str,
    schedule_expression: str,
    timezone: str,
    target_arn: str,
    scheduler_role_arn: str,
    description: str,
) -> None:
    """Create an EventBridge Scheduler schedule, or update it if it already exists."""
    target = {
        "Arn": target_arn,
        "RoleArn": scheduler_role_arn,
        "Input": '{"source": "scheduled"}',
    }
    flexible_time_window = {"Mode": "OFF"}

    try:
        client.get_schedule(Name=name)
        client.update_schedule(
            Name=name,
            ScheduleExpression=schedule_expression,
            ScheduleExpressionTimezone=timezone,
            FlexibleTimeWindow=flexible_time_window,
            Target=target,
            Description=description,
        )
        print(f"  ✓ Schedule updated: {name}  ({schedule_expression}, {timezone})")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            client.create_schedule(
                Name=name,
                ScheduleExpression=schedule_expression,
                ScheduleExpressionTimezone=timezone,
                FlexibleTimeWindow=flexible_time_window,
                Target=target,
                Description=description,
            )
            print(f"  ✓ Schedule created: {name}  ({schedule_expression}, {timezone})")
        else:
            raise


# ---------------------------------------------------------------------------
# Instructions (printed, not executed)
# ---------------------------------------------------------------------------

def print_secret_instructions() -> None:
    print(f"\n[7/9] Create Secrets Manager secret (do this manually in the console or CLI)")
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
  ΓöÇΓöÇ Lambda deployment ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

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
  ΓöÇΓöÇ EventBridge Scheduler ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

  Editor-in-Chief ΓÇö Monday 9am NZT (Pacific/Auckland):

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

  Archivist (Whakaaro) ΓÇö nightly 11pm NZT:

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
    print("Magic Content Engine ΓÇö mce-second-brain Infrastructure Setup")
    print("=" * 60)
    print(f"Region : {REGION}")
    print(f"Bucket : s3://{S3_BUCKET}")
    print(f"Tables : {CHECKPOINTS_TABLE}, {TOPIC_COVERAGE_TABLE}, "
          f"{DEDUPLICATION_TABLE}, {HELD_ITEMS_TABLE}")

    # S3
    try:
        create_s3_bucket()
    except Exception as exc:
        print(f"  Γ£ù S3 bucket creation failed: {exc}", file=sys.stderr)
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
            msg = f"  Γ£ù {fn.__name__} failed: {exc}"
            print(msg, file=sys.stderr)
            errors.append(msg)

    # IAM roles
    iam_errors = create_iam_roles()
    errors.extend(iam_errors)

    # EventBridge Scheduler
    try:
        sts = boto3.client("sts", region_name=REGION)
        account_id = sts.get_caller_identity()["Account"]
        scheduler_role_arn = (
            f"arn:aws:iam::{account_id}:role/eventbridge-scheduler-role"
        )
        create_eventbridge_schedules(
            account_id=account_id,
            scheduler_role_arn=scheduler_role_arn,
        )
    except Exception as exc:
        msg = f"  ✗ create_eventbridge_schedules failed: {exc}"
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
