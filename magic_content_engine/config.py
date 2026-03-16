"""Configuration loaded from environment variables with sensible defaults."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- AWS / S3 ---
S3_BUCKET: str = os.getenv("S3_BUCKET", "magic-content-dev")
S3_KEY_PREFIX: str = os.getenv("S3_KEY_PREFIX", "output/")

# --- Steering ---
STEERING_BASE_PATH: str = os.getenv("STEERING_BASE_PATH", ".kiro/steering/")

# --- Bedrock model IDs ---
HAIKU_MODEL_ID: str = os.getenv(
    "HAIKU_MODEL_ID", "anthropic.claude-3-5-haiku-20241022-v1:0"
)
SONNET_MODEL_ID: str = os.getenv(
    "SONNET_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0"
)

# --- Scoring ---
RELEVANCE_THRESHOLD: int = int(os.getenv("RELEVANCE_THRESHOLD", "3"))

# --- Screenshot ---
SCREENSHOT_VIEWPORT_W: int = int(os.getenv("SCREENSHOT_VIEWPORT_W", "1440"))
SCREENSHOT_VIEWPORT_H: int = int(os.getenv("SCREENSHOT_VIEWPORT_H", "900"))
SCREENSHOT_WAIT_S: int = int(os.getenv("SCREENSHOT_WAIT_S", "3"))

# --- Retry ---
MAX_RETRY_ATTEMPTS: int = int(os.getenv("MAX_RETRY_ATTEMPTS", "3"))

# --- Logging ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# --- SES ---
SES_SENDER_EMAIL: str = os.getenv("SES_SENDER_EMAIL", "")
SES_RECIPIENT_EMAIL: str = os.getenv("SES_RECIPIENT_EMAIL", "")

# --- Output paths ---
HELD_OUTPUT_PATH: str = os.getenv("HELD_OUTPUT_PATH", "./output/held/")
REVIEW_OUTPUT_PATH: str = os.getenv("REVIEW_OUTPUT_PATH", "./output/review/")

# --- dev.to ---
DEVTO_API_KEY: str = os.getenv("DEVTO_API_KEY", "")
DEVTO_USERNAME: str = os.getenv("DEVTO_USERNAME", "")
