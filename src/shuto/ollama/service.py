"""Stopping and starting Ollama (taskkill / osascript / pkill, the tray app, `ollama serve`)."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shuto.core import platform as core_platform
from shuto.ollama import env
from shuto.ollama.errors import StoreError, StoreUnsupported

START_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 10.0


def _is_up(client: Any) -> bool:
    try:
        client.version()
    except Exception:
        return False
    return True


def _wait(
    until: Callable[[], bool],
    timeout: float,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> bool:
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    end = clock() + timeout
    while True:
        if until():
            return True
        if clock() >= end:
            return False
        sleep(0.5)


def _tray_app() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"


def _mac_app() -> Path:
    return Path("/Applications/Ollama.app")


def can_start(os_name: str | None = None) -> bool:
    """Is there an Ollama on this computer that shuto can start?"""
    os_ = os_name or core_platform.os_name()
    if shutil.which("ollama"):
        return True
    return _tray_app().is_file() if os_ == "windows" else _mac_app().exists()


def _current_user() -> str | None:
    """The Windows account name for taskkill's USERNAME filter; None if unknown."""
    user = os.environ.get("USERNAME") or ""
    if not user:
        try:
            user = getpass.getuser()
        except Exception:
            return None
    domain = os.environ.get("USERDOMAIN") or ""
    return "\\".join([domain, user]) if domain else user


def _taskkill(image: str, user: str, force: bool) -> None:
    cmd = ["taskkill"] + (["/F"] if force else []) + ["/IM", image]
    env._run(cmd + ["/FI", f"USERNAME eq {user}"])


def stop_ollama(client: Any, os_name: str | None = None, timeout: float = STOP_TIMEOUT_S) -> None:
    """Close Ollama politely first, then by force if it is still running. No-op if not running."""
    os_ = os_name or core_platform.os_name()
    if os_ == "windows":
        user = _current_user()  # only this user's Ollama, never another signed-in user's
        if user:
            for force in (False, True):
                for image in ("ollama app.exe", "ollama.exe"):
                    _taskkill(image, user, force)
                if not force:
                    _wait(lambda: not _is_up(client), timeout)
    elif os_ == "macos":
        env._run(["osascript", "-e", 'quit app "Ollama"'])
        if not _wait(lambda: not _is_up(client), timeout):
            env._run(["pkill", "-x", "ollama"])
    else:
        raise StoreUnsupported(env.LINUX_TEXT)
    if not _wait(lambda: not _is_up(client), 5):
        raise StoreError("Ollama would not close. Quit Ollama yourself, then try again.")


_ENV_KEEP = frozenset(
    """SYSTEMROOT WINDIR SYSTEMDRIVE COMSPEC PATHEXT PATH USERPROFILE LOCALAPPDATA APPDATA
    PROGRAMDATA PROGRAMFILES PROGRAMFILES(X86) PROGRAMW6432 TEMP TMP TMPDIR HOMEDRIVE HOMEPATH
    HOME USER USERNAME USERDOMAIN LOGNAME SHELL LANG LC_ALL NUMBER_OF_PROCESSORS
    PROCESSOR_ARCHITECTURE""".split()
)
_ENV_KEEP_PREFIX = ("OLLAMA_", "XDG_", "CUDA_", "HIP_", "ROCR_", "HSA_", "LC_")


def _venv_dirs() -> list[str]:
    dirs = [os.environ.get("VIRTUAL_ENV", "")]
    if sys.prefix != sys.base_prefix:
        dirs.append(sys.prefix)
    return [os.path.normcase(os.path.abspath(d)) for d in dirs if d]


def _clean_env() -> dict[str, str]:
    """A trimmed copy of os.environ for the Ollama that shuto starts: an allow-list, PATH
    without shuto's virtual environment, and no API keys or VIRTUAL_ENV."""
    venvs = _venv_dirs()
    out: dict[str, str] = {}
    for key, value in os.environ.items():
        up = key.upper()
        if up not in _ENV_KEEP and not up.startswith(_ENV_KEEP_PREFIX):
            continue
        if up == "PATH":
            keep = []
            for part in value.split(os.pathsep):
                norm = os.path.normcase(os.path.abspath(part)) if part else ""
                if part and not any(norm == v or norm.startswith(v + os.sep) for v in venvs):
                    keep.append(part)
            value = os.pathsep.join(keep)
        out[key] = value
    return out


def _spawn(cmd: list[str]) -> None:
    kw: dict[str, Any] = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # detached, new group, no window
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=_clean_env(),
            **kw,
        )
    except OSError:
        raise StoreError(
            "shuto could not start Ollama. Open Ollama yourself, then try again."
        ) from None


def start_ollama(client: Any, os_name: str | None = None, timeout: float = START_TIMEOUT_S) -> None:
    """Start Ollama (it inherits the new OLLAMA_MODELS) and wait until it answers."""
    os_ = os_name or core_platform.os_name()
    exe = shutil.which("ollama")
    if os_ == "windows":
        app = _tray_app()
        if app.is_file():
            _spawn([str(app)])
        elif exe:
            _spawn([exe, "serve"])
        else:
            raise StoreError("shuto could not find Ollama. Open Ollama from the Start menu.")
    elif os_ == "macos":
        if env._run(["open", "-a", "Ollama"]).returncode != 0:
            if not exe:
                raise StoreError("shuto could not find Ollama. Open it from Applications.")
            _spawn([exe, "serve"])
    else:
        raise StoreUnsupported(env.LINUX_TEXT)
    if not _wait(lambda: _is_up(client), timeout):
        raise StoreError(
            "Ollama did not start again within 30 seconds. Open Ollama yourself "
            "(Start menu on Windows, Applications on a Mac), then check this page."
        )
