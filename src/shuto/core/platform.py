"""OS name and a guarded subprocess probe (shared by hardware detection)."""

from __future__ import annotations

import platform
import subprocess
from collections.abc import Callable


def os_name() -> str:
    s = platform.system().lower()
    return {"darwin": "macos"}.get(s, s or "unknown")


def run(
    run: Callable, cmd: list[str], notes: list[str], label: str, timeout: float = 5
) -> str | None:
    """Run a probe; return stdout or None (adding a note when noteworthy) on failure."""
    try:
        p = run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return None  # tool simply not installed: not worth a note
    except subprocess.TimeoutExpired:
        notes.append(f"{label}: timed out")
        return None
    except OSError as e:
        notes.append(f"{label}: {e}")
        return None
    if getattr(p, "returncode", 0) != 0:
        return None
    return p.stdout or ""
