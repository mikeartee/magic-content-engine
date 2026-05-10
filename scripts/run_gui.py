#!/usr/bin/env python3
"""
Bullpen Web GUI — entry point with system tray icon.

Usage:
    Double-click bullpen.bat

A tray icon appears in the system tray. Right-click to open browser or quit.

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
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Filled circle background
    draw.ellipse([2, 2, 62, 62], fill=(26, 26, 46))
    # Simple "B" letterform
    draw.rectangle([14, 12, 22, 52], fill=(77, 159, 255))
    draw.rectangle([22, 12, 42, 22], fill=(77, 159, 255))
    draw.rectangle([22, 30, 40, 34], fill=(77, 159, 255))
    draw.rectangle([22, 42, 42, 52], fill=(77, 159, 255))
    draw.ellipse([36, 12, 48, 34], fill=(77, 159, 255))
    draw.ellipse([36, 32, 48, 52], fill=(77, 159, 255))
    return img


def main() -> None:
    port = int(os.environ.get("GUI_PORT", "5000"))
    url = f"http://127.0.0.1:{port}"

    # Import Flask app
    from scripts.gui.app import app
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    # Try to set up tray icon
    tray_icon = None
    try:
        import pystray

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

        tray_icon = pystray.Icon(
            name="bullpen",
            icon=_make_icon(),
            title="Bullpen",
            menu=menu,
        )

        # Run tray icon in its own thread
        tray_thread = threading.Thread(target=tray_icon.run, daemon=True)
        tray_thread.start()

    except Exception as e:
        print(f"Tray icon unavailable: {e}", file=sys.stderr)

    # Open browser once Flask is ready (in background thread)
    def _open_browser():
        import time, urllib.request
        for _ in range(40):
            time.sleep(0.25)
            try:
                urllib.request.urlopen(f"{url}/api/health", timeout=1)
                webbrowser.open(url)
                return
            except Exception:
                continue

    threading.Thread(target=_open_browser, daemon=True).start()

    print(f"Bullpen starting at {url}")
    if tray_icon:
        print("Tray icon active — right-click to open or quit.")
    else:
        print("Press Ctrl+C to stop.")

    # Run Flask on the main thread
    try:
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    except OSError as exc:
        if "address already in use" in str(exc).lower() or getattr(exc, "errno", None) in (98, 10048):
            print(f"ERROR: Port {port} is already in use.", file=sys.stderr)
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
