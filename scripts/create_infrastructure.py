#!/usr/bin/env python3
"""Create AWS infrastructure for the Magic Content Engine.

Run once before first deployment:
  python scripts/create_infrastructure.py

What this script does:
  1. Creates the DynamoDB table (magic-content-engine)
  2. Prints the Secrets Manager secret you need to create manually
  3. Prints the Lambda deployment commands
  4. Prints the EventBridge Scheduler command

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
TABLE_NAME = "magic-content-engine"
SECRET_NAME = "magic-content-engine/credentials"
LAMBDA_FUNCTION = "magic-content-engine"
S3_BUCKET = "magic-content-dev"


def create_dynamodb_table() -> None:
    print(f"\n[1/4] Creating DynamoDB table: {TABLE_NAME}")
    client = boto3.client("dynamodb", region_name=REGION)

    try:
        client.describe_table(TableName=TABLE_NAME)
        print(f"  ✓ Table already exists — skipping")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    client.create_table(
        TableName=TABLE_NAME,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
    )

    # Wait for the table to be active
    waiter = client.get_waiter("table_exists")
    print("  Waiting for table to become active...")
    waiter.wait(TableName=TABLE_NAME)
    print(f"  ✓ Table created: {TABLE_NAME}")


def print_secret_instructions() -> None:
    print(f"\n[2/4] Create Secrets Manager secret (do this manually in the console or CLI)")
    secret_value = {
        "github_token":       "ghp_YOUR_GITHUB_TOKEN_HERE",
        "devto_api_key":      "YOUR_DEVTO_API_KEY_HERE",
        "devto_username":     "YOUR_DEVTO_USERNAME_HERE",
        "ses_sender_email":   "YOUR_VERIFIED_SES_SENDER@example.com",
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
  If you haven't published any dev.to posts yet, devto_api_key
  and devto_username can be any non-empty string — the engine
  handles the empty-posts case gracefully.
""")


def print_lambda_instructions() -> None:
    print(f"\n[3/4] Deploy the Lambda function")
    print(f"""
  Build and deploy:

  # Install dependencies into a package directory
  pip install -r requirements-lambda.txt -t ./package/ --quiet

  # Add source code
  cp -r magic_content_engine ./package/
  cp lambda_handler.py ./package/
  cp -r .kiro ./package/

  # Zip it up
  cd package && zip -r ../magic-content-engine.zip . && cd ..

  # Create the Lambda function (first time)
  aws lambda create-function \\
    --function-name {LAMBDA_FUNCTION} \\
    --runtime python3.12 \\
    --handler lambda_handler.handler \\
    --timeout 900 \\
    --memory-size 1024 \\
    --zip-file fileb://magic-content-engine.zip \\
    --role arn:aws:iam::YOUR_ACCOUNT_ID:role/magic-content-engine-role \\
    --region {REGION} \\
    --environment "Variables={{\\
      S3_BUCKET={S3_BUCKET},\\
      DYNAMODB_TABLE={TABLE_NAME},\\
      SECRETS_NAME={SECRET_NAME},\\
      STEERING_BASE_PATH=/var/task/.kiro/steering/,\\
      LOG_LEVEL=INFO\\
    }}"

  # Update the function code (subsequent deploys)
  aws lambda update-function-code \\
    --function-name {LAMBDA_FUNCTION} \\
    --zip-file fileb://magic-content-engine.zip \\
    --region {REGION}

  Required IAM role permissions (attach to Lambda execution role):
    - bedrock:InvokeModel
    - dynamodb:GetItem, PutItem, DeleteItem, Query (table: {TABLE_NAME})
    - secretsmanager:GetSecretValue (secret: {SECRET_NAME})
    - s3:PutObject (bucket: {S3_BUCKET}, prefix: output/*)
    - ses:SendEmail (optional, for embargo notifications)
    - logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents
""")


def print_eventbridge_instructions() -> None:
    print(f"\n[4/4] Create EventBridge Scheduler rule (Monday 9am UTC)")
    print(f"""
  aws scheduler create-schedule \\
    --name magic-content-engine-weekly \\
    --schedule-expression "cron(0 9 ? * MON *)" \\
    --schedule-expression-timezone "UTC" \\
    --flexible-time-window "Mode=OFF" \\
    --target "{{\\
      \\"Arn\\": \\"arn:aws:lambda:{REGION}:YOUR_ACCOUNT_ID:function:{LAMBDA_FUNCTION}\\",\\
      \\"RoleArn\\": \\"arn:aws:iam::YOUR_ACCOUNT_ID:role/eventbridge-scheduler-role\\",\\
      \\"Input\\": \\"{{\\\\\\"source\\\\\\": \\\\\\"scheduled\\\\\\"}}\"\\
    }}" \\
    --region {REGION}

  The scheduler role needs:
    - lambda:InvokeFunction on the {LAMBDA_FUNCTION} function

  To test immediately (manual trigger):
  aws lambda invoke \\
    --function-name {LAMBDA_FUNCTION} \\
    --payload '{{"source": "manual"}}' \\
    --region {REGION} \\
    response.json && cat response.json
""")


def main() -> None:
    print("Magic Content Engine — Infrastructure Setup")
    print("=" * 50)

    try:
        create_dynamodb_table()
    except Exception as exc:
        print(f"  ✗ DynamoDB creation failed: {exc}", file=sys.stderr)
        print("  Check your AWS credentials and try again.", file=sys.stderr)

    print_secret_instructions()
    print_lambda_instructions()
    print_eventbridge_instructions()

    print("\n" + "=" * 50)
    print("Done. Follow the instructions above to complete deployment.")
    print("Run the Lambda test invoke to verify before Monday's scheduled run.")


if __name__ == "__main__":
    main()
