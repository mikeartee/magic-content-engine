"""
Entry point for the Bullpen Web GUI.

Usage:
    python scripts/run_gui.py

Environment variables:
    GUI_PORT   Port to listen on (default: 5000)

Loads .env via python-dotenv before starting the Flask development server.
"""

import os
import sys

# Load .env — python-dotenv is listed in pyproject.toml dependencies.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    print(
        "Warning: python-dotenv is not installed. "
        "Install it with: pip install python-dotenv",
        file=sys.stderr,
    )

# Resolve the project root so that `scripts/gui` is importable regardless of
# the working directory the user launches from.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts.gui.app import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("GUI_PORT", 5000))

    try:
        app.run(host="127.0.0.1", port=port, debug=False)
    except OSError as exc:
        # Port-in-use handling will be completed in Task 16.
        # For now, log a descriptive message and exit non-zero.
        print(
            f"Error: could not bind to port {port}. "
            f"Is another process already using it? ({exc})",
            file=sys.stderr,
        )
        sys.exit(1)
