"""Saving OLLAMA_MODELS where new programs find it: the Windows user environment (registry),
a macOS launchctl setting plus login item. Linux is configured by an administrator."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any

from shuto.core import platform as core_platform
from shuto.ollama.errors import StoreError, StoreUnsupported

ENV_NAME = "OLLAMA_MODELS"
LAUNCH_AGENT = "com.shuto.ollama-models.plist"
LINUX_TEXT = (
    "On Linux, Ollama runs as a system service, so this needs an administrator. "
    "Ask a helper to follow the steps on the Help page, under For helpers."
)

MACHINE_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
MACHINE_TEXT = (
    "The models folder is set for the whole computer by an administrator, so shuto cannot "
    "change it. Ask someone with administrator rights, or a helper, to change it "
    "(see the Help page, under For helpers)."
)
MACHINE_RESET_TEXT = (
    "The whole computer is set up by an administrator to keep models in another folder, so "
    "going back to the standard folder would not work: Ollama would use the administrator's "
    "folder again after the next sign-in. Ask someone with administrator rights, or a helper, "
    "to change it (see the Help page, under For helpers)."
)


def _win_read(machine: bool) -> str | None:
    """OLLAMA_MODELS as stored in Windows for this user (machine=False) or for everyone."""
    reg = _winreg()
    root, sub = (
        (reg.HKEY_LOCAL_MACHINE, MACHINE_KEY) if machine else (reg.HKEY_CURRENT_USER, "Environment")
    )
    try:
        with reg.OpenKey(root, sub, 0, reg.KEY_READ) as key:
            value, kind = reg.QueryValueEx(key, ENV_NAME)
    except OSError:
        return None
    if not value:
        return None
    if kind == getattr(reg, "REG_EXPAND_SZ", 2):
        value = os.path.expandvars(str(value))  # e.g. %USERPROFILE%/models
    return str(value)


def _run(cmd: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _winreg() -> Any:
    import winreg

    return winreg


def _win_broadcast() -> None:
    """Tell running programs (Explorer) that environment variables changed."""
    import ctypes

    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
        0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result)
    )


def _win_write(value: str | None) -> None:
    reg = _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, "Environment", 0, reg.KEY_SET_VALUE) as key:
            if value is None:
                try:
                    reg.DeleteValue(key, ENV_NAME)
                except FileNotFoundError:
                    pass
            else:
                reg.SetValueEx(key, ENV_NAME, 0, reg.REG_SZ, value)
    except OSError:
        raise StoreError("Windows would not let shuto save this setting.") from None
    try:
        _win_broadcast()
    except Exception:
        pass  # only affects programs that are already open


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / LAUNCH_AGENT


def _mac_write(value: str | None) -> None:
    plist = _plist_path()
    if value is None:
        _run(["launchctl", "unsetenv", ENV_NAME])
        try:
            plist.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise StoreError("shuto could not remove its start-up setting.") from None
        return
    done = _run(["launchctl", "setenv", ENV_NAME, value])
    if done.returncode != 0:
        raise StoreError("macOS would not let shuto save this setting.")
    try:
        plist.parent.mkdir(parents=True, exist_ok=True)
        with plist.open("wb") as fh:
            plistlib.dump(
                {
                    "Label": LAUNCH_AGENT.removesuffix(".plist"),
                    "ProgramArguments": ["/bin/launchctl", "setenv", ENV_NAME, value],
                    "RunAtLoad": True,
                },
                fh,
            )
    except OSError:
        raise StoreError("shuto could not save the start-up setting for this folder.") from None


def _save_env(value: str | None, os_name: str | None) -> None:
    os_ = os_name or core_platform.os_name()
    if os_ == "windows":
        _win_write(value)
    elif os_ == "macos":
        _mac_write(value)
    else:
        raise StoreUnsupported(LINUX_TEXT)
    if value is None:
        os.environ.pop(ENV_NAME, None)
    else:
        os.environ[ENV_NAME] = value


def set_models_dir(path: Path | str, os_name: str | None = None) -> None:
    """Save OLLAMA_MODELS for this user (no admin rights) and for this shuto process."""
    _save_env(str(path), os_name)


def reset_models_dir(os_name: str | None = None) -> None:
    """Undo set_models_dir: Ollama goes back to its own default folder."""
    _save_env(None, os_name)
