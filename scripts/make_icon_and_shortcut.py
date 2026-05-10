#!/usr/bin/env python3
"""
Generate a .ico file from the Bullpen logo and create a desktop shortcut.

Run once:
    python scripts/make_icon_and_shortcut.py

Creates:
    scripts/gui/static/bullpen.ico     — multi-size Windows icon
    ~/Desktop/Bullpen.lnk              — shortcut pointing at bullpen.bat
"""

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.run_gui import _make_icon


def make_ico() -> Path:
    """Render the logo at multiple sizes and save as .ico."""
    base = _make_icon()
    ico_path = _REPO_ROOT / "scripts" / "gui" / "static" / "bullpen.ico"
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base.save(ico_path, format="ICO", sizes=sizes)
    print(f"Wrote {ico_path}")
    return ico_path


def make_shortcut(ico_path: Path) -> None:
    """Create a desktop shortcut to bullpen.bat with the Bullpen icon."""
    try:
        from win32com.client import Dispatch
    except ImportError:
        print("pywin32 not installed — skipping shortcut creation.")
        print("Install with: pip install pywin32")
        return

    desktop = Path(os.path.expanduser("~/Desktop"))
    shortcut_path = desktop / "Bullpen.lnk"
    bat_path = _REPO_ROOT / "bullpen.bat"

    shell = Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(str(shortcut_path))
    shortcut.TargetPath = str(bat_path)
    shortcut.WorkingDirectory = str(_REPO_ROOT)
    shortcut.IconLocation = str(ico_path)
    shortcut.Description = "Bullpen — Magic Content Engine"
    shortcut.WindowStyle = 7  # minimised
    shortcut.save()
    print(f"Wrote {shortcut_path}")


if __name__ == "__main__":
    ico = make_ico()
    make_shortcut(ico)
    print("\nDone. A 'Bullpen' shortcut with the logo is now on your desktop.")
