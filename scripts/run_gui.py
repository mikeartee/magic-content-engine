#!/usr/bin/env python3
"""
Bullpen Web GUI — entry point with system tray icon.

Usage:
    python scripts/run_gui.py

Double-click bullpen.bat to launch. A tray icon appears in the system tray.
Right-click it to open the browser or quit.

Environment variables:
    GUI_PORT            Port to listen on (default: 5000)
    VAULT_PATH          Path to your second-brain vault
    DEVTO_API_KEY       dev.to API key for publishing
    AWS_DEFAULT_REGION  AWS region for DynamoDB (default: ap-southeast-2)
"""

import os
import sys
import threading
import webbrowser
from pathlib import Path

# Load .env from the repo root before importing the app
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE)
except ImportError:
    pass

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _make_icon():
    """Create a simple coloured square as the tray icon."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (64, 64), color=(26, 26, 46))  # dark navy
    draw = ImageDraw.Draw(img)
    # Draw a simple "B" shape using rectangles
    draw.rectangle([12, 10, 20, 54], fill=(77, 159, 255))   # vertical bar
    draw.rectangle([20, 10, 40, 22], fill=(77, 159, 255))   # top arm
    draw.rectangle([20, 30, 38, 42], fill=(77, 159, 255))   # middle arm
    draw.rectangle([20, 42, 40, 54], fill=(77, 159, 255))   # bottom arm
    draw.rectangle([40, 10, 44, 32], fill=(77, 159, 255))   # top curve
    draw.rectangle([38, 42, 44, 54], fill=(77, 159, 255))   # bottom curve
    return img


def _run_flask(port: int, stop_event: threading.Event) -> None:
    """Run Flask in a background thread."""
    from scripts.gui.app import app
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)  # suppress per-request logs when using tray

    try:
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    except OSError as exc:
        if "address already in use" in str(exc).lower() or getattr(exc, "errno", None) in (98, 10048):
            print(f"ERROR: Port {port} is already in use. Set GUI_PORT to a different port.", file=sys.stderr)
        stop_event.set()


def main() -> None:
    port = int(os.environ.get("GUI_PORT", "5000"))
    url = f"http://127.0.0.1:{port}"

    stop_event = threading.Event()

    # Start Flask in background thread
    flask_thread = threading.Thread(target=_run_flask, args=(port, stop_event), daemon=True)
    flask_thread.start()

    # Wait briefly for Flask to start, then open browser
    import time
    for _ in range(20):
        time.sleep(0.25)
        try:
            import urllib.request
            urllib.request.urlopen(f"{url}/api/health", timeout=1)
            break
        except Exception:
            continue

    webbrowser.open(url)

    # Build tray icon
    try:
        import pystray

        icon_image = _make_icon()

        def on_open(icon, item):
            webbrowser.open(url)

        def on_quit(icon, item):
            icon.stop()
            os._exit(0)

        menu = pystray.Menu(
            pystray.MenuItem("Open Bullpen", on_open, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        )

        icon = pystray.Icon(
            name="bullpen",
            icon=icon_image,
            title="Bullpen — Magic Content Engine",
            menu=menu,
        )

        print(f"Bullpen running at {url}")
        print("Right-click the tray icon to open or quit.")
        icon.run()  # blocks until quit

    except ImportError:
        # pystray not available — fall back to blocking on the Flask thread
        print(f"Bullpen running at {url}")
        print("Press Ctrl+C to stop.")
        try:
            flask_thread.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
