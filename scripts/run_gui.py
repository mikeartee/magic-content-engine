#!/usr/bin/env python3
"""
Bullpen Web GUI — entry point.

Usage:
    python scripts/run_gui.py

Environment variables:
    GUI_PORT    Port to listen on (default: 5000)
    DEVTO_API_KEY  dev.to API key for publishing
    AWS_DEFAULT_REGION  AWS region for DynamoDB (default: ap-southeast-2)
"""

import os
import sys
from pathlib import Path

# Load .env from the repo root before importing the app
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE)
except ImportError:
    pass  # python-dotenv not installed — rely on environment variables

# Ensure the repo root is on sys.path so `scripts.gui.app` is importable
# regardless of the working directory the user launches from.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main() -> None:
    port = int(os.environ.get("GUI_PORT", "5000"))

    # Import here so .env is loaded before the app module reads config
    from scripts.gui.app import app

    print(f"Starting Bullpen Web GUI on http://127.0.0.1:{port}")
    print("Press Ctrl+C to stop.")

    try:
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    except OSError as exc:
        if "address already in use" in str(exc).lower() or getattr(exc, "errno", None) in (98, 10048):
            print(
                f"ERROR: Port {port} is already in use. "
                f"Set GUI_PORT to a different port and try again.",
                file=sys.stderr,
            )
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
