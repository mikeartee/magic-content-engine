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
    """Bullpen logo — blue circle with a white bull silhouette and drop shadow."""
    from PIL import Image, ImageDraw, ImageFilter

    size = 128
    img = Image.new("RGBA", (size, size), color=(0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Blue circle background
    blue = (26, 77, 204, 255)
    draw.ellipse([2, 2, size - 2, size - 2], fill=blue)

    # Bull silhouette on a separate layer so we can drop-shadow it
    bull_layer = Image.new("RGBA", (size, size), color=(0, 0, 0, 0))
    bull = ImageDraw.Draw(bull_layer)
    white = (255, 255, 255, 255)

    # Horns (triangles)
    bull.polygon([(32, 52), (45, 44), (54, 60)], fill=white)
    bull.polygon([(96, 52), (83, 44), (74, 60)], fill=white)
    # Head
    bull.ellipse([37, 48, 91, 90], fill=white)
    # Muzzle
    bull.ellipse([48, 74, 80, 104], fill=white)
    # Eyes and nostrils (blue, to punch back through)
    bull.ellipse([50, 64, 58, 72], fill=blue)
    bull.ellipse([70, 64, 78, 72], fill=blue)
    bull.ellipse([56, 86, 62, 94], fill=blue)
    bull.ellipse([66, 86, 72, 94], fill=blue)

    # Build drop shadow: solid black silhouette, offset, blurred
    alpha = bull_layer.split()[3]
    shadow = Image.new("RGBA", (size, size), color=(0, 0, 0, 0))
    black = Image.new("RGBA", (size, size), color=(0, 0, 0, 140))
    shadow.paste(black, (4, 4), alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=3))

    # Composite: circle, shadow (clipped to circle), bull, clipped to circle
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([2, 2, size - 2, size - 2], fill=255)

    img.paste(shadow, (0, 0), Image.composite(shadow.split()[3], Image.new("L", (size, size), 0), mask))
    img.paste(bull_layer, (0, 0), Image.composite(bull_layer.split()[3], Image.new("L", (size, size), 0), mask))

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
