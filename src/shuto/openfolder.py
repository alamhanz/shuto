"""Open a folder in the computer's file manager (a local action, never reachable from a URL)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

NO_DESKTOP = (
    "shuto could not open the folder on this computer. Open it yourself from the path shown."
)


def open_folder(path: str | Path) -> str | None:
    """Show `path` in the file manager. Returns None on success, else a plain message."""
    folder = Path(path)
    try:
        if sys.platform == "win32":
            os.startfile(str(folder))  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])  # noqa: S603, S607
        else:
            opener = shutil.which("xdg-open")
            if opener is None:
                return NO_DESKTOP
            subprocess.Popen(  # noqa: S603
                [opener, str(folder)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except (OSError, ValueError):
        return NO_DESKTOP
    return None
